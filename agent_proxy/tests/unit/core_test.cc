#include <atomic>
#include <future>
#include <iostream>

#include "adapters/e2b/connect_codec.h"
#include "core/executor.h"
#include "tools/arguments.h"

using namespace ap;
using Json = nlohmann::json;
static int count = 0;
void expect(bool value, const char* message) {
  ++count;
  if (!value)
    throw std::runtime_error(message);
}
template <class F>
void rejects(Code code, F fn) {
  try {
    fn();
  } catch (const Error& e) {
    expect(e.code == code, "wrong error code");
    return;
  }
  throw std::runtime_error("expected rejection");
}
int main() {
  try {
    auto tool = parse_tool("bash", R"({"command":"exit 2","env":{"X":"abc"}})", std::nullopt);
    expect(std::get<Bash>(tool).timeout_ms == 60000, "default execution timeout");
    rejects(Code::InvalidArgument,
            [] { parse_tool("write", R"({"path":"relative","content":"x"})", {}); });
    rejects(Code::InvalidArgument,
            [] { parse_tool("read", R"({"path":"/tmp/a","paht":"x"})", {}); });
    rejects(Code::InvalidArgument, [] { parse_tool("read", R"({"path":"/tmp/a"})", 1); });
    rejects(Code::InvalidArgument, [] { parse_tool("bash", "[]", {}); });
    rejects(Code::InvalidArgument, [] { parse_tool("bash", R"({"command":"x"})", 0); });
    rejects(Code::Unsupported, [] { parse_tool("python", "{}", {}); });
    expect(replace_once("hello world", Edit{"/tmp/a", "world", "agent"}) == "hello agent",
           "literal edit");
    rejects(Code::EditConflict, [] { replace_once("aaaa", Edit{"/tmp/a", "aa", "b"}); });
    rejects(Code::EditConflict, [] { replace_once("abc", Edit{"/tmp/a", "z", "b"}); });
    rejects(Code::PayloadTooLarge, [] {
      replace_once(std::string(kFileLimit, 'a'),
                   Edit{"/tmp/a", std::string(kFileLimit, 'a'), std::string(kFileLimit + 1, 'b')});
    });
    rejects(Code::Unsupported, [] { validate_text(std::string("\xff", 1)); });
    expect(base64_decode(base64_encode(std::string("a\0b", 3))) == std::string("a\0b", 3),
           "binary base64 roundtrip");
    rejects(Code::BackendProtocolError, [] { base64_decode("%%%%"); });
    std::vector<Json> events;
    ConnectDecoder split([&](const Json& j) { events.push_back(j); });
    auto wire =
        connect_frame({{"event", {{"start", {{"pid", 1}}}}}}) + connect_frame(Json::object(), 2);
    for (char c : wire)
      split.feed(&c, 1);
    split.finish();
    expect(events.size() == 1, "fragmented stream");
    rejects(Code::BackendProtocolError, [&] { split.feed("x", 1); });
    for (uint8_t flags : {0, 2}) {
      ConnectDecoder null_frame([](const Json&) {});
      auto null_wire = connect_frame(Json(nullptr), flags);
      rejects(Code::BackendProtocolError,
              [&] { null_frame.feed(null_wire.data(), null_wire.size()); });
    }
    ConnectDecoder no_trailer([](const Json&) {});
    rejects(Code::BackendProtocolError, [&] { no_trailer.finish(); });
    ConnectDecoder bad([](const Json&) {});
    auto err = connect_frame({{"error", {{"code", "unavailable"}}}}, 2);
    rejects(Code::BackendUnavailable, [&] { bad.feed(err.data(), err.size()); });
    ConnectDecoder malformed([](const Json&) {});
    std::string huge("\0\x7f\xff\xff\xff", 5);
    rejects(Code::BackendProtocolError, [&] { malformed.feed(huge.data(), huge.size()); });
    SandboxLocks locks;
    auto first = locks.acquire("a");
    expect(first != nullptr, "first lease");
    expect(!locks.acquire("a"), "exclusive lease");
    expect(locks.acquire("b") != nullptr, "different sandbox");
    first.reset();
    expect(locks.acquire("a") != nullptr, "lease released");
    std::promise<void> running, release, finished;
    auto wait = release.get_future().share();
    {
      Executor pool(1, 1);
      expect(pool.submit([&] {
        running.set_value();
        wait.wait();
      }),
             "submit active");
      running.get_future().wait();
      expect(pool.submit([&] { finished.set_value(); }), "submit queued");
      expect(!pool.submit([] {}), "bounded queue");
      release.set_value();
    }
    finished.get_future().wait();
    std::promise<void> blocked, unblock, expired;
    auto unblock_future = unblock.get_future().share();
    auto expired_future = expired.get_future();
    {
      Executor pool(1, 1);
      pool.submit([&] {
        blocked.set_value();
        unblock_future.wait();
      });
      blocked.get_future().wait();
      pool.submit([&] { expired.set_value(); }, Clock::now() + std::chrono::milliseconds(20));
      const auto expiration = expired_future.wait_for(std::chrono::seconds(2));
      unblock.set_value();
      expect(expiration == std::future_status::ready,
             "queued request expires while worker is busy");
    }
    OperationContext ctx;
    ctx.deadline = Clock::now() - std::chrono::milliseconds(1);
    rejects(Code::DeadlineExceeded, [&] { ctx.check(); });
    std::cout << count << " core checks passed\n";
    return 0;
  } catch (const std::exception& e) {
    std::cerr << "FAIL: " << e.what() << '\n';
    return 1;
  }
}
