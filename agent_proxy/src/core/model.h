#pragma once

#include <chrono>
#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <variant>

namespace ap {
using Clock = std::chrono::steady_clock;
enum class Code : int {
  Ok = 0,
  InvalidArgument = 1001,
  Unsupported = 1002,
  PayloadTooLarge = 1003,
  NotFound = 1101,
  InvalidState = 1102,
  EditConflict = 1103,
  SandboxBusy = 1104,
  Unauthenticated = 1201,
  PermissionDenied = 1202,
  RateLimited = 1301,
  ResourceExhausted = 1302,
  DeadlineExceeded = 1401,
  Cancelled = 1402,
  BackendUnavailable = 1501,
  BackendError = 1502,
  BackendProtocolError = 1503,
  InternalError = 1901
};
enum class Source { Proxy, Control, Envd };
enum class Outcome { NotStarted, Completed, Unknown };
enum class Retry { No, Safe, Reconcile };
enum class State { Unknown, Running, Paused, Deleted };
enum class Basis { Unspecified, BackendAck, EnvdReady, StateObserved };
enum class Phase { Preflight, Operation, Readiness, Cleanup };
enum class Termination { Unknown, Exited, TimedOut, Cancelled, Signalled };

struct Error : std::runtime_error {
  Code code;
  Source source;
  std::string resource;
  int http_status = 0;
  std::string backend_code;
  Retry retry = Retry::No;
  std::optional<uint32_t> retry_after_ms;
  explicit Error(Code c, std::string message, Source s = Source::Proxy, std::string r = "")
      : std::runtime_error(std::move(message)), code(c), source(s), resource(std::move(r)) {}
};
struct Ref {
  std::string backend, id;
};
struct Info {
  Ref ref;
  State state = State::Unknown;
  std::string template_id, expires_at, observed_at;
  std::map<std::string, std::string> metadata;
  std::optional<bool> envd_ready;
};
struct Bash {
  std::string command, cwd;
  std::map<std::string, std::string> env;
  uint32_t timeout_ms = 60000;
};
struct Read {
  std::string path;
};
struct Write {
  std::string path, content;
};
struct Edit {
  std::string path, old_string, new_string;
};
using Tool = std::variant<Bash, Read, Write, Edit>;
struct BashResult {
  std::string out, err;
  std::optional<int32_t> exit_code;
  Termination termination = Termination::Unknown;
  bool out_truncated = false, err_truncated = false, termination_confirmed = false;
  uint32_t effective_timeout_ms = 0;
  std::optional<uint32_t> pid;
};
struct ReadResult {
  std::string content;
};
struct WriteResult {
  uint64_t bytes = 0;
};
struct EditResult {
  uint64_t bytes = 0;
  uint32_t replacements = 0;
};
using ToolResult = std::variant<std::monostate, BashResult, ReadResult, WriteResult, EditResult>;
struct Result {
  std::optional<Info> info;
  ToolResult tool;
  bool already = false;
  Basis basis = Basis::Unspecified;
};
struct CreateSpec {
  std::string backend, template_id;
  uint32_t ttl_seconds = 300;
  bool wait_ready = true;
  std::map<std::string, std::string> env, metadata;
};
struct ResumeSpec {
  Ref ref;
  uint32_t ttl_seconds = 300;
  bool wait_ready = true;
};
struct Timing {
  uint64_t queue_us = 0, preflight_us = 0, operation_us = 0, readiness_us = 0, cleanup_us = 0,
           backend_total_us = 0;
  uint32_t requests = 0;
};
struct OperationContext {
  Clock::time_point start, deadline;
  std::function<bool()> cancelled = [] { return false; };
  Timing timing;
  Outcome outcome = Outcome::NotStarted;
  int64_t remaining_ms() const {
    return std::chrono::duration_cast<std::chrono::milliseconds>(deadline - Clock::now()).count();
  }
  void check(bool cleanup = false) const {
    if (!cleanup && cancelled())
      throw Error(Code::Cancelled, "request cancelled");
    if (remaining_ms() <= 0)
      throw Error(Code::DeadlineExceeded, "operation deadline exceeded");
  }
};
inline uint64_t micros(Clock::time_point a, Clock::time_point b) {
  return std::chrono::duration_cast<std::chrono::microseconds>(b - a).count();
}
std::string utc_now();
constexpr size_t kFileLimit = 8 * 1024 * 1024;
constexpr size_t kOutputLimit = 1024 * 1024;
constexpr size_t kRpcLimit = 16 * 1024 * 1024;
}  // namespace ap
