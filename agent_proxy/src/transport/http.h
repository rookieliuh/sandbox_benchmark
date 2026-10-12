#pragma once
#include <vector>

#include "core/model.h"
namespace ap {
struct HttpRequest {
  std::string method = "GET", url, body;
  std::vector<std::string> headers;
  Source source = Source::Control;
  std::string resource = "sandbox";
  Phase phase = Phase::Operation;
  bool mutation = false, cleanup = false;
  bool multipart = false;
  size_t max_body = kFileLimit;
  int64_t timeout_ms = 0;
  std::function<void(const char*, size_t)> on_data;
};
struct HttpResponse {
  int status = 0;
  std::string body;
  std::optional<uint32_t> retry_after_ms;
};
class HttpClient {
 public:
  HttpResponse perform(const HttpRequest&, OperationContext&) const;
  static void require_success(const HttpResponse&, const HttpRequest&, OperationContext&);
};
std::string url_encode(const std::string&);
Error connect_error(const std::string& code);
}  // namespace ap
