#include "transport/http.h"

#include <curl/curl.h>

#include <algorithm>
#include <cctype>
#include <exception>
#include <limits>
#include <memory>

namespace ap {
namespace {
struct Easy {
  CURL* handle = curl_easy_init();
  ~Easy() {
    if (handle)
      curl_easy_cleanup(handle);
  }
};
struct Transfer {
  CURL* curl;
  const HttpRequest& req;
  OperationContext& ctx;
  HttpResponse response;
  std::exception_ptr exception;
};
size_t write_cb(char* data, size_t size, size_t count, void* opaque) noexcept {
  auto& t = *static_cast<Transfer*>(opaque);
  const auto n = size * count;
  try {
    long status = 0;
    curl_easy_getinfo(t.curl, CURLINFO_RESPONSE_CODE, &status);
    if (t.req.on_data && status >= 200 && status < 300)
      t.req.on_data(data, n);
    else {
      auto limit = status >= 200 && status < 300 ? t.req.max_body : 65536;
      if (t.response.body.size() + n > limit)
        throw Error(Code::PayloadTooLarge, "backend response exceeds size limit", t.req.source,
                    t.req.resource);
      t.response.body.append(data, n);
    }
    return n;
  } catch (...) {
    t.exception = std::current_exception();
    return 0;
  }
}
size_t header_cb(char* data, size_t size, size_t count, void* opaque) noexcept {
  auto& t = *static_cast<Transfer*>(opaque);
  auto n = size * count;
  try {
    std::string line(data, n), lower = line;
    std::transform(lower.begin(), lower.end(), lower.begin(),
                   [](unsigned char c) { return std::tolower(c); });
    if (lower.rfind("retry-after:", 0) == 0) {
      const auto value = line.substr(12);
      size_t end = 0;
      auto seconds = std::stoull(value, &end);
      if (value.find_first_not_of(" \t\r\n", end) == std::string::npos)
        t.response.retry_after_ms =
            static_cast<uint32_t>(std::min<uint64_t>(seconds, 4294967) * 1000);
    }
  } catch (...) { /* Optional header; ignore malformed values and HTTP dates. */
  }
  return n;
}
int progress_cb(void* opaque, curl_off_t, curl_off_t, curl_off_t, curl_off_t) noexcept {
  auto& t = *static_cast<Transfer*>(opaque);
  try {
    t.ctx.check(t.req.cleanup);
    return 0;
  } catch (...) {
    t.exception = std::current_exception();
    return 1;
  }
}
struct Measure {
  OperationContext& ctx;
  Phase phase;
  Clock::time_point start = Clock::now();
  ~Measure() {
    const auto us = micros(start, Clock::now());
    ctx.timing.backend_total_us += us;
    switch (phase) {
      case Phase::Preflight:
        ctx.timing.preflight_us += us;
        break;
      case Phase::Operation:
        ctx.timing.operation_us += us;
        break;
      case Phase::Readiness:
        ctx.timing.readiness_us += us;
        break;
      case Phase::Cleanup:
        ctx.timing.cleanup_us += us;
        break;
    }
  }
};
}  // namespace
std::string url_encode(const std::string& value) {
  static const char* hex = "0123456789ABCDEF";
  std::string out;
  for (unsigned char c : value) {
    if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '-' ||
        c == '_' || c == '.' || c == '~')
      out += c;
    else {
      out += '%';
      out += hex[c >> 4];
      out += hex[c & 15];
    }
  }
  return out;
}
HttpResponse HttpClient::perform(const HttpRequest& req, OperationContext& ctx) const {
  ctx.check(req.cleanup);
  // Each worker owns its handle and connection cache; curl_easy_reset preserves connections.
  thread_local Easy easy;
  auto* curl = easy.handle;
  if (!curl)
    throw Error(Code::InternalError, "curl initialization failed");
  curl_easy_reset(curl);
  Transfer transfer{curl, req, ctx, {}, nullptr};
  const auto timeout = std::min<int64_t>(ctx.remaining_ms(),
                                         req.timeout_ms > 0 ? req.timeout_ms : ctx.remaining_ms());
  if (timeout <= 0)
    throw Error(Code::DeadlineExceeded, "operation deadline exceeded");
  curl_easy_setopt(curl, CURLOPT_URL, req.url.c_str());
  curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, req.method.c_str());
  curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
  curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 0L);
  // libcurl may transparently replay a request after a reused connection fails.
  // Mutations use a fresh connection so an ambiguous POST is never replayed.
  // Read-only requests still reuse the per-worker connection cache.
  curl_easy_setopt(curl, CURLOPT_FRESH_CONNECT, (req.mutation || req.cleanup) ? 1L : 0L);
  curl_easy_setopt(curl, CURLOPT_PROTOCOLS, CURLPROTO_HTTP | CURLPROTO_HTTPS);
  curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS,
                   static_cast<long>(std::min<int64_t>(3000, timeout)));
  curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, static_cast<long>(timeout));
  curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_cb);
  curl_easy_setopt(curl, CURLOPT_WRITEDATA, &transfer);
  curl_easy_setopt(curl, CURLOPT_HEADERFUNCTION, header_cb);
  curl_easy_setopt(curl, CURLOPT_HEADERDATA, &transfer);
  curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, progress_cb);
  curl_easy_setopt(curl, CURLOPT_XFERINFODATA, &transfer);
  curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
  curl_slist* raw_headers = nullptr;
  for (const auto& header : req.headers) {
    auto* next = curl_slist_append(raw_headers, header.c_str());
    if (!next) {
      curl_slist_free_all(raw_headers);
      throw Error(Code::InternalError, "header allocation failed");
    }
    raw_headers = next;
  }
  std::unique_ptr<curl_slist, decltype(&curl_slist_free_all)> headers(raw_headers,
                                                                      curl_slist_free_all);
  curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers.get());
  std::unique_ptr<curl_mime, decltype(&curl_mime_free)> mime(nullptr, curl_mime_free);
  if (req.multipart) {
    mime.reset(curl_mime_init(curl));
    if (!mime)
      throw Error(Code::InternalError, "multipart allocation failed");
    auto* part = curl_mime_addpart(mime.get());
    if (!part || curl_mime_name(part, "file") != CURLE_OK ||
        curl_mime_filename(part, "upload") != CURLE_OK ||
        curl_mime_data(part, req.body.data(), req.body.size()) != CURLE_OK)
      throw Error(Code::InternalError, "multipart initialization failed");
    curl_easy_setopt(curl, CURLOPT_MIMEPOST, mime.get());
  } else if (req.method == "POST" || req.method == "PUT") {
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, req.body.data());
    curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE_LARGE, static_cast<curl_off_t>(req.body.size()));
  }
  if (req.mutation)
    ctx.outcome = Outcome::Unknown;
  if (req.phase == Phase::Operation && !req.cleanup)
    ctx.on_dispatch(false);
  ++ctx.timing.requests;
  Measure measure{ctx, req.phase};
  auto code = curl_easy_perform(curl);
  if (transfer.exception)
    std::rethrow_exception(transfer.exception);
  if (code != CURLE_OK) {
    auto mapped =
        code == CURLE_OPERATION_TIMEDOUT ? Code::DeadlineExceeded : Code::BackendUnavailable;
    // Do not copy curl error text: URLs, tokens or backend content can appear in diagnostics.
    throw Error(
        mapped,
        code == CURLE_OPERATION_TIMEDOUT ? "backend request timed out" : "backend transport failed",
        req.source, req.resource);
  }
  long status = 0;
  curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
  transfer.response.status = static_cast<int>(status);
  if (req.phase == Phase::Operation && !req.cleanup)
    ctx.on_dispatch(true);
  return std::move(transfer.response);
}
void HttpClient::require_success(const HttpResponse& r, const HttpRequest& req,
                                 OperationContext& ctx) {
  if (r.status >= 200 && r.status < 300)
    return;
  Code code = Code::BackendError;
  switch (r.status) {
    case 400:
    case 422:
      code = Code::InvalidArgument;
      break;
    case 401:
      code = Code::Unauthenticated;
      break;
    case 403:
      code = Code::PermissionDenied;
      break;
    case 404:
      code = Code::NotFound;
      break;
    case 409:
      code = Code::InvalidState;
      break;
    case 413:
      code = Code::PayloadTooLarge;
      break;
    case 429:
      code = Code::RateLimited;
      break;
    case 502:
    case 503:
      code = Code::BackendUnavailable;
      break;
    case 504:
      code = Code::DeadlineExceeded;
      break;
    case 507:
      code = Code::ResourceExhausted;
      break;
  }
  if (req.mutation && r.status >= 400 && r.status < 500)
    ctx.outcome = Outcome::Completed;
  if (ctx.outcome == Outcome::NotStarted && req.phase == Phase::Operation)
    ctx.outcome = Outcome::Completed;
  Error e(code, "backend rejected request", req.source, req.resource);
  e.http_status = r.status;
  e.retry_after_ms = r.retry_after_ms;
  throw e;
}
Error connect_error(const std::string& code) {
  Code c = Code::BackendError;
  if (code == "invalid_argument")
    c = Code::InvalidArgument;
  else if (code == "not_found")
    c = Code::NotFound;
  else if (code == "unauthenticated")
    c = Code::Unauthenticated;
  else if (code == "permission_denied")
    c = Code::PermissionDenied;
  else if (code == "resource_exhausted")
    c = Code::ResourceExhausted;
  else if (code == "unavailable")
    c = Code::BackendUnavailable;
  else if (code == "deadline_exceeded")
    c = Code::DeadlineExceeded;
  else if (code == "canceled")
    c = Code::Cancelled;
  else if (code == "unimplemented")
    c = Code::Unsupported;
  else if (code == "failed_precondition" || code == "aborted")
    c = Code::InvalidState;
  Error e(c, "envd RPC failed", Source::Envd, "process");
  e.backend_code = code;
  return e;
}
}  // namespace ap
