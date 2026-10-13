#pragma once

#include <memory>
#include <string>

namespace ap {
enum class RpcPerfKind {
  CreateSandbox,
  RunStep,
  PauseSandbox,
  ResumeSandbox,
  DeleteSandbox,
  GetStatus
};
class RpcPerf;

// Call before creating worker threads. Empty name disables collection without touching SHM.
// An explicit name requires an already-created, dedicated /agent_proxy_* SHM object.
void initialize_rpc_perf(const std::string& shm_name);
// Call only after all RPCs and worker pools have finished; flushes the SDK's final snapshot.
void shutdown_rpc_perf() noexcept;
std::shared_ptr<RpcPerf> start_rpc_perf(RpcPerfKind kind);
// Accepts the wire ErrorCode: ERROR_OK (1) becomes SDK success (0).
void end_rpc_perf(const std::shared_ptr<RpcPerf>& point, int wire_code) noexcept;

// Place after the bRPC ClosureGuard, so even unexpected unwinding records failure
// before done runs. Release only when ownership is transferred to the queued work.
class RpcPerfCompletion {
 public:
  explicit RpcPerfCompletion(const std::shared_ptr<RpcPerf>& point) : point_(point) {}
  ~RpcPerfCompletion();
  RpcPerfCompletion(const RpcPerfCompletion&) = delete;
  RpcPerfCompletion& operator=(const RpcPerfCompletion&) = delete;
  void release() noexcept {
    point_.reset();
  }

 private:
  std::shared_ptr<RpcPerf> point_;
};
}  // namespace ap
