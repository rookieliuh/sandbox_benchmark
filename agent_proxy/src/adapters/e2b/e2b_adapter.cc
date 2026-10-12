#include "adapters/e2b/e2b_adapter.h"

#include <algorithm>
#include <array>
#include <regex>
#include <thread>

#include "adapters/e2b/connect_codec.h"
#include "tools/arguments.h"

namespace ap {
using Json = nlohmann::json;
namespace {
Json parse(const std::string& body, Source source) {
  try {
    return Json::parse(body);
  } catch (const Json::exception&) {
    throw Error(Code::BackendProtocolError, "invalid backend JSON", source);
  }
}
std::string required_string(const Json& j, const char* key, Source source) {
  if (!j.contains(key) || !j[key].is_string() || j[key].get_ref<const std::string&>().empty())
    throw Error(Code::BackendProtocolError, "required backend field missing or invalid", source);
  return j[key].get<std::string>();
}
std::array<int, 3> version(const std::string& text) {
  std::smatch match;
  if (!std::regex_match(text, match, std::regex("v?([0-9]{1,4})\\.([0-9]{1,4})\\.([0-9]{1,4})")))
    throw Error(Code::Unsupported, "unrecognized envd version", Source::Envd);
  return {std::stoi(match[1]), std::stoi(match[2]), std::stoi(match[3])};
}
void append_output(std::string& dest, bool& truncated, const std::string& data) {
  auto keep = std::min(data.size(), kOutputLimit - dest.size());
  dest.append(data.data(), keep);
  truncated = truncated || keep < data.size();
}
}  // namespace
HttpRequest E2BAdapter::control(const std::string& method, const std::string& path, Phase phase,
                                bool mutation) const {
  HttpRequest r;
  r.method = method;
  r.url = config_.api_url + path;
  r.phase = phase;
  r.mutation = mutation;
  r.headers = {"X-API-Key: " + config_.api_key, "Content-Type: application/json",
               "Accept: application/json"};
  r.max_body = 1024 * 1024;
  return r;
}
HttpRequest E2BAdapter::envd(const Connection& c, const std::string& method,
                             const std::string& path, Phase phase, bool mutation, bool rpc) const {
  HttpRequest r;
  r.method = method;
  r.url = c.url + path;
  r.phase = phase;
  r.mutation = mutation;
  r.source = Source::Envd;
  r.resource = "file";
  r.headers = {"E2b-Sandbox-Id: " + c.info.ref.id, "E2b-Sandbox-Port: 49983", "Expect:"};
  if (!c.token.empty())
    r.headers.push_back("X-Access-Token: " + c.token);
  if (rpc && !c.user.empty())
    r.headers.push_back("Authorization: Basic " + base64_encode(c.user + ":"));
  if (rpc)
    r.headers.push_back("Connect-Protocol-Version: 1");
  return r;
}
Json E2BAdapter::control_json(HttpRequest& req, OperationContext& ctx) const {
  auto response = http_.perform(req, ctx);
  HttpClient::require_success(response, req, ctx);
  if (response.status != (req.method == "POST" ? 201 : 200))
    throw Error(Code::BackendProtocolError, "unexpected control API success status",
                Source::Control);
  auto j = parse(response.body, Source::Control);
  if (!j.is_object())
    throw Error(Code::BackendProtocolError, "expected sandbox object", Source::Control);
  return j;
}
Info E2BAdapter::info(const Json& j, const Ref& ref, State fallback) const {
  Info out;
  out.ref = {ref.backend, required_string(j, "sandboxID", Source::Control)};
  try {
    validate_ref(out.ref);
  } catch (const Error&) {
    throw Error(Code::BackendProtocolError, "invalid sandbox ID from backend", Source::Control);
  }
  if (!ref.id.empty() && out.ref.id != ref.id)
    throw Error(Code::BackendProtocolError, "sandbox ID mismatch", Source::Control);
  const auto state = j.value("state", "");
  if (state == "running")
    out.state = State::Running;
  else if (state == "paused")
    out.state = State::Paused;
  else if (state == "pausing" || state == "resuming" || state == "creating" || state == "deleting")
    out.state = State::Unknown;
  else if (state.empty())
    out.state = fallback;
  else
    throw Error(Code::BackendProtocolError, "unknown backend sandbox state", Source::Control);
  out.template_id = j.value("templateID", "");
  out.expires_at = j.value("endAt", "");
  out.observed_at = utc_now();
  if (j.contains("metadata") && !j["metadata"].is_null())
    out.metadata = j["metadata"].get<std::map<std::string, std::string>>();
  return out;
}
E2BAdapter::Connection E2BAdapter::connection(const Json& j, const Ref& ref) const {
  Connection c;
  c.info = info(j, ref, State::Running);
  auto v = version(required_string(j, "envdVersion", Source::Control));
  if (v < std::array<int, 3>{0, 3, 0})
    throw Error(Code::Unsupported, "envd >= 0.3.0 required for noninteractive commands",
                Source::Envd);
  c.octet = v >= std::array<int, 3>{0, 5, 7};
  if (j.contains("envdAccessToken") && !j["envdAccessToken"].is_null())
    c.token = j["envdAccessToken"].get<std::string>();
  if (c.token.find_first_of("\r\n") != std::string::npos)
    throw Error(Code::BackendProtocolError, "invalid envd token", Source::Control);
  c.user = config_.user;
  if (c.user.empty() && v < std::array<int, 3>{0, 4, 0})
    c.user = "user";
  c.url = config_.envd_url_pattern;
  if (c.url.empty()) {
    auto domain = config_.sandbox_domain;
    if (j.contains("domain") && !j["domain"].is_null())
      domain = j["domain"].get<std::string>();
    if (!std::regex_match(domain, std::regex("[A-Za-z0-9][A-Za-z0-9.-]*")))
      throw Error(Code::BackendProtocolError, "valid sandbox domain is required", Source::Control);
    c.url = config_.envd_scheme + "://49983-" + c.info.ref.id + "." + domain;
  } else {
    const std::string marker = "{sandbox_id}";
    auto pos = c.url.find(marker);
    if (pos != std::string::npos)
      c.url.replace(pos, marker.size(), url_encode(c.info.ref.id));
  }
  while (!c.url.empty() && c.url.back() == '/')
    c.url.pop_back();
  return c;
}
Json E2BAdapter::details(const Ref& ref, OperationContext& ctx, Phase phase) const {
  auto r = control("GET", "/sandboxes/" + url_encode(ref.id), phase);
  return control_json(r, ctx);
}
Json E2BAdapter::unary(const Connection& c, const std::string& path, const Json& body,
                       OperationContext& ctx, Phase phase, bool cleanup) const {
  auto r = envd(c, "POST", path, phase, false, true);
  r.cleanup = cleanup;
  r.body = body.dump();
  r.max_body = 1024 * 1024;
  if (cleanup)
    r.timeout_ms = 2000;
  r.headers.push_back("Content-Type: application/json");
  r.headers.push_back("Connect-Timeout-Ms: " +
                      std::to_string(std::max<int64_t>(1, ctx.remaining_ms())));
  r.resource = path.find("process.") != std::string::npos ? "process" : "file";
  auto response = http_.perform(r, ctx);
  if (response.status < 200 || response.status >= 300) {
    Json err = Json::parse(response.body, nullptr, false);
    if (err.is_object() && err.contains("code") && err["code"].is_string()) {
      auto e = connect_error(err["code"]);
      e.resource = r.resource;
      e.http_status = response.status;
      throw e;
    }
    if (response.status == 404)
      throw Error(Code::Unsupported, "envd RPC route not available", Source::Envd);
    HttpClient::require_success(response, r, ctx);
  }
  auto result = parse(response.body, Source::Envd);
  if (response.status != 200)
    throw Error(Code::BackendProtocolError, "unexpected envd RPC success status", Source::Envd);
  if (!result.is_object())
    throw Error(Code::BackendProtocolError, "expected unary RPC object", Source::Envd);
  return result;
}
void E2BAdapter::ready(const Connection& c, OperationContext& ctx, Result& out) const {
  out.info->envd_ready = false;
  auto& health = ctx.envd_health;
  int64_t delay_ms = 250;
  try {
    while (true) {
      ctx.check();
      auto r = envd(c, "GET", "/health", Phase::Readiness);
      r.resource = "sandbox";
      r.timeout_ms = std::min<int64_t>(1000, ctx.remaining_ms());
      r.max_body = 65536;
      ++health.attempts;
      health.last_http_status = 0;
      health.last_transport_code.clear();
      try {
        auto response = http_.perform(r, ctx);
        health.last_http_status = response.status;
        ++health.http_counts[std::to_string(response.status)];
        if (response.status == 200) {
          out.info->envd_ready = true;
          out.basis = Basis::EnvdReady;
          return;
        }
        if (response.status != 502 && response.status != 503 && response.status != 504 &&
            response.status != 404)
          HttpClient::require_success(response, r, ctx);
      } catch (const Error& e) {
        if (e.code != Code::BackendUnavailable && e.code != Code::DeadlineExceeded)
          throw;
        health.last_transport_code = e.backend_code.empty()
                                         ? "AP_" + std::to_string(static_cast<int>(e.code))
                                         : e.backend_code;
      }
      ctx.check();
      auto start = Clock::now();
      std::this_thread::sleep_for(
          std::chrono::milliseconds(std::min<int64_t>(delay_ms, ctx.remaining_ms())));
      ctx.timing.readiness_us += micros(start, Clock::now());
      delay_ms = std::min<int64_t>(1000, delay_ms * 2);
    }
  } catch (const Error& e) {
    if (e.code != Code::DeadlineExceeded)
      throw;
    Error error(Code::DeadlineExceeded,
                "envd readiness deadline exceeded; attempts=" + std::to_string(health.attempts) +
                    "; last_http_status=" + std::to_string(health.last_http_status) +
                    "; last_transport_code=" +
                    (health.last_transport_code.empty() ? "none" : health.last_transport_code),
                Source::Envd, "sandbox");
    error.http_status = health.last_http_status;
    error.backend_code = health.last_transport_code;
    throw error;
  }
}
void E2BAdapter::create(const CreateSpec& spec, OperationContext& ctx, Result& out) {
  auto r = control("POST", "/sandboxes", Phase::Operation, true);
  // The REST schema uses autoResume.policy, not the SDK's lifecycle object.
  r.body = Json{{"templateID", spec.template_id},
                {"timeout", spec.ttl_seconds},
                {"envVars", spec.env},
                {"metadata", spec.metadata},
                {"secure", true},
                {"autoPause", false},
                {"autoResume", {{"policy", "off"}}}}
               .dump();
  Json j;
  try {
    j = control_json(r, ctx);
  } catch (Error& e) {
    if (e.code == Code::NotFound)
      e.resource = "template";
    throw;
  }
  out.info = info(j, {spec.backend, {}}, State::Running);
  ctx.outcome = Outcome::Completed;
  out.basis = Basis::BackendAck;
  if (spec.wait_ready)
    ready(connection(j, out.info->ref), ctx, out);
}
void E2BAdapter::status(const Ref& ref, OperationContext& ctx, Result& out) {
  auto j = details(ref, ctx, Phase::Operation);
  out.info = info(j, ref);
  if (out.info->state == State::Running)
    ready(connection(j, ref), ctx, out);
  ctx.outcome = Outcome::Completed;
  out.basis = Basis::StateObserved;
}
void E2BAdapter::pause(const Ref& ref, OperationContext& ctx, Result& out) {
  auto r = control("POST", "/sandboxes/" + url_encode(ref.id) + "/pause", Phase::Operation, true);
  // Empty body requests full memory pause in the reference API (no filesystem-only downgrade).
  r.body = "{}";
  auto response = http_.perform(r, ctx);
  if (response.status == 409) {
    ctx.outcome = Outcome::Completed;
    auto current = info(details(ref, ctx, Phase::Preflight), ref);
    if (current.state == State::Paused) {
      out.info = current;
      out.already = true;
      out.basis = Basis::StateObserved;
      return;
    }
  }
  HttpClient::require_success(response, r, ctx);
  if (response.status != 204)
    throw Error(Code::BackendProtocolError, "unexpected pause success status", Source::Control);
  // A POST acknowledgement alone is not a verified PAUSED state.
  while (true) {
    ctx.check();
    auto current = info(details(ref, ctx, Phase::Readiness), ref);
    out.info = current;
    if (current.state == State::Paused)
      break;
    std::this_thread::sleep_for(
        std::chrono::milliseconds(std::min<int64_t>(50, ctx.remaining_ms())));
  }
  ctx.outcome = Outcome::Completed;
  out.basis = Basis::StateObserved;
}
void E2BAdapter::resume(const ResumeSpec& spec, OperationContext& ctx, Result& out) {
  auto r =
      control("POST", "/sandboxes/" + url_encode(spec.ref.id) + "/connect", Phase::Operation, true);
  r.body = Json{{"timeout", spec.ttl_seconds}}.dump();
  auto response = http_.perform(r, ctx);
  HttpClient::require_success(response, r, ctx);
  if (response.status != 200 && response.status != 201)
    throw Error(Code::BackendProtocolError, "unexpected connect success status", Source::Control);
  auto j = parse(response.body, Source::Control);
  out.info = info(j, spec.ref, State::Running);
  out.already = response.status == 200;
  ctx.outcome = Outcome::Completed;
  out.basis = Basis::BackendAck;
  if (spec.wait_ready)
    ready(connection(j, spec.ref), ctx, out);
}
void E2BAdapter::remove(const Ref& ref, bool ignore_missing, OperationContext& ctx, Result& out) {
  auto r = control("DELETE", "/sandboxes/" + url_encode(ref.id), Phase::Operation, true);
  auto response = http_.perform(r, ctx);
  if (response.status == 404 && ignore_missing)
    out.already = true;
  else {
    HttpClient::require_success(response, r, ctx);
    if (response.status != 204)
      throw Error(Code::BackendProtocolError, "unexpected delete success status", Source::Control);
  }
  // Confirm control-plane absence. Never infer physical cleanup from an HTTP 204 alone.
  while (!out.already) {
    ctx.check();
    auto check = control("GET", "/sandboxes/" + url_encode(ref.id), Phase::Readiness);
    auto observed = http_.perform(check, ctx);
    if (observed.status == 404)
      break;
    HttpClient::require_success(observed, check, ctx);
    std::this_thread::sleep_for(
        std::chrono::milliseconds(std::min<int64_t>(50, ctx.remaining_ms())));
  }
  ctx.outcome = Outcome::Completed;
  out.basis = Basis::StateObserved;
}
std::string E2BAdapter::read_file(const Connection& c, const std::string& path,
                                  OperationContext& ctx) const {
  auto query = "/files?path=" + url_encode(path);
  if (!c.user.empty())
    query += "&username=" + url_encode(c.user);
  auto r = envd(c, "GET", query, Phase::Operation);
  auto response = http_.perform(r, ctx);
  HttpClient::require_success(response, r, ctx);
  if (response.status != 200)
    throw Error(Code::BackendProtocolError, "unexpected file read success status", Source::Envd,
                "file");
  validate_text(response.body);
  return response.body;
}
void E2BAdapter::write_file(const Connection& c, const std::string& path,
                            const std::string& content, OperationContext& ctx) const {
  // Envd upload creates parents automatically. Enforce v1's existing-parent contract first.
  const auto slash = path.find_last_of('/');
  const auto parent = slash == 0 ? "/" : path.substr(0, slash);
  const auto stat =
      unary(c, "/filesystem.Filesystem/Stat", {{"path", parent}}, ctx, Phase::Preflight);
  if (!stat.contains("entry") || !stat["entry"].is_object())
    throw Error(Code::BackendProtocolError, "missing filesystem entry", Source::Envd, "file");
  const auto& entry = stat["entry"];
  if (!entry.contains("type") || !(entry["type"] == "FILE_TYPE_DIRECTORY" || entry["type"] == 2))
    throw Error(Code::InvalidArgument, "parent is not a directory", Source::Envd, "file");
  auto query = "/files?path=" + url_encode(path);
  if (!c.user.empty())
    query += "&username=" + url_encode(c.user);
  auto r = envd(c, "POST", query, Phase::Operation, true);
  r.body = content;
  r.multipart = !c.octet;
  r.max_body = 1024 * 1024;
  if (c.octet)
    r.headers.push_back("Content-Type: application/octet-stream");
  auto response = http_.perform(r, ctx);
  HttpClient::require_success(response, r, ctx);
  if (response.status != 200)
    throw Error(Code::BackendProtocolError, "unexpected upload success status", Source::Envd,
                "file");
  auto j = parse(response.body, Source::Envd);
  if (!j.is_array() || j.empty())
    throw Error(Code::BackendProtocolError, "missing upload result", Source::Envd, "file");
  ctx.outcome = Outcome::Completed;
}
void E2BAdapter::bash(const Connection& c, const Bash& command, OperationContext& ctx,
                      Result& out) const {
  out.tool = BashResult{};
  auto& result = std::get<BashResult>(out.tool);
  ctx.check();
  auto budget = std::min<int64_t>(command.timeout_ms, ctx.remaining_ms() - 2000);
  if (budget <= 0)
    throw Error(Code::DeadlineExceeded, "insufficient command budget including cleanup reserve");
  result.effective_timeout_ms = static_cast<uint32_t>(budget);
  auto process =
      Json{{"cmd", "/bin/bash"}, {"args", {"-l", "-c", command.command}}, {"envs", command.env}};
  if (!command.cwd.empty())
    process["cwd"] = command.cwd;
  auto r = envd(c, "POST", "/process.Process/Start", Phase::Operation, true, true);
  r.resource = "process";
  r.body = connect_frame({{"process", process}, {"stdin", false}});
  r.headers.push_back("Content-Type: application/connect+json");
  r.headers.push_back("Connect-Timeout-Ms: " + std::to_string(budget));
  r.timeout_ms = budget;
  bool started = false, ended = false;
  ConnectDecoder decoder([&](const Json& j) {
    if (!j.contains("event") || !j["event"].is_object())
      throw Error(Code::BackendProtocolError, "missing process event", Source::Envd);
    const auto& event = j["event"];
    if (event.size() != 1)
      throw Error(Code::BackendProtocolError, "invalid process event", Source::Envd);
    if (event.contains("start")) {
      if (started || ended || !event["start"].contains("pid") ||
          !event["start"]["pid"].is_number_unsigned())
        throw Error(Code::BackendProtocolError, "invalid start event", Source::Envd);
      auto pid = event["start"]["pid"].get<uint64_t>();
      if (pid == 0 || pid > UINT32_MAX)
        throw Error(Code::BackendProtocolError, "invalid process PID", Source::Envd);
      result.pid = static_cast<uint32_t>(pid);
      ctx.on_dispatch(true);
      started = true;
    } else if (event.contains("keepalive")) {
      if (ended)
        throw Error(Code::BackendProtocolError, "keepalive after exit", Source::Envd);
    } else if (event.contains("data")) {
      if (!started || ended || !event["data"].is_object())
        throw Error(Code::BackendProtocolError, "unexpected output event", Source::Envd);
      const auto& d = event["data"];
      if (d.contains("stdout"))
        append_output(result.out, result.out_truncated,
                      base64_decode(d["stdout"].get<std::string>()));
      else if (d.contains("stderr"))
        append_output(result.err, result.err_truncated,
                      base64_decode(d["stderr"].get<std::string>()));
      else
        throw Error(Code::BackendProtocolError, "unsupported process output", Source::Envd);
    } else if (event.contains("end")) {
      if (!started || ended)
        throw Error(Code::BackendProtocolError, "unexpected exit event", Source::Envd);
      ended = true;
      const auto& end = event["end"];
      if (!end.is_object())
        throw Error(Code::BackendProtocolError, "invalid exit event", Source::Envd);
      // Proto3 JSON omits zero-valued exitCode.
      auto exit = end.value("exitCode", int64_t{0});
      if (exit < INT32_MIN || exit > INT32_MAX)
        throw Error(Code::BackendProtocolError, "invalid exit code", Source::Envd);
      result.exit_code = static_cast<int32_t>(exit);
      result.termination_confirmed = true;
      result.termination =
          end.value("exited", false) ? Termination::Exited : Termination::Signalled;
      // Envd puts os/exec's "exit status N" in error for ordinary nonzero exits.
      // An end event is a process result, not an infrastructure error.
    } else
      throw Error(Code::BackendProtocolError, "unknown process event", Source::Envd);
  });
  r.on_data = [&](const char* data, size_t n) { decoder.feed(data, n); };
  try {
    auto response = http_.perform(r, ctx);
    if (response.status == 404) {
      ctx.outcome = Outcome::Completed;
      throw Error(Code::Unsupported, "envd process route not available", Source::Envd, "process");
    }
    HttpClient::require_success(response, r, ctx);
    decoder.finish();
    if (!ended)
      throw Error(Code::BackendProtocolError, "stream completed without process exit", Source::Envd,
                  "process");
    ctx.outcome = Outcome::Completed;
  } catch (const Error& e) {
    if (e.code == Code::DeadlineExceeded)
      result.termination = Termination::TimedOut;
    if (e.code == Code::Cancelled)
      result.termination = Termination::Cancelled;
    if (result.termination_confirmed)
      ctx.outcome = Outcome::Completed;
    // Any interrupted stream may leave a process alive, not only explicit timeouts.
    if (result.pid && !result.termination_confirmed && ctx.remaining_ms() > 0) {
      try {
        unary(c, "/process.Process/SendSignal",
              {{"process", {{"pid", *result.pid}}}, {"signal", "SIGNAL_SIGKILL"}}, ctx,
              Phase::Cleanup, true);
        // Acknowledging a signal does not prove exit; keep termination_confirmed false.
      } catch (...) { /* Preserve original failure and uncertainty. */
      }
    }
    throw;
  }
}
void E2BAdapter::run(const Ref& ref, const Tool& tool, OperationContext& ctx, Result& out) {
  auto j = details(ref, ctx, Phase::Preflight);
  if (info(j, ref).state != State::Running)
    throw Error(Code::InvalidState, "sandbox is not running", Source::Control, "sandbox");
  auto c = connection(j, ref);
  std::visit(
      [&](const auto& t) {
        using T = std::decay_t<decltype(t)>;
        if constexpr (std::is_same_v<T, Bash>)
          bash(c, t, ctx, out);
        else if constexpr (std::is_same_v<T, Read>) {
          out.tool = ReadResult{read_file(c, t.path, ctx)};
          ctx.outcome = Outcome::Completed;
        } else if constexpr (std::is_same_v<T, Write>) {
          write_file(c, t.path, t.content, ctx);
          out.tool = WriteResult{t.content.size()};
        } else {
          auto content = replace_once(read_file(c, t.path, ctx), t);
          write_file(c, t.path, content, ctx);
          out.tool = EditResult{content.size(), 1};
        }
      },
      tool);
}
}  // namespace ap
