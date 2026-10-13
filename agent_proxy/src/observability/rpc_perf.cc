#include "observability/rpc_perf.h"

#include <atomic>
#include <cstdio>
#include <stdexcept>

#include "agent_proxy.pb.h"

#define SPDIAG_PERF_DEF_FILE "agent_proxy_perf_points.def"
#define SPDIAG_PROGRAM_NAME "agent-proxy"
#include "spdiag/auto_perf.h"

namespace ap {
namespace {
namespace pb = benchmark::agent::v1;
// Written before worker creation and after worker joins; unchanged during request handling.
bool enabled = false;
PerfKey key_for(RpcPerfKind kind) {
  switch (kind) {
    case RpcPerfKind::CreateSandbox:
      return PerfKey::AGENT_PROXY_CREATE_SANDBOX_FULL;
    case RpcPerfKind::RunStep:
      return PerfKey::AGENT_PROXY_RUN_STEP_FULL;
    case RpcPerfKind::PauseSandbox:
      return PerfKey::AGENT_PROXY_PAUSE_SANDBOX_FULL;
    case RpcPerfKind::ResumeSandbox:
      return PerfKey::AGENT_PROXY_RESUME_SANDBOX_FULL;
    case RpcPerfKind::DeleteSandbox:
      return PerfKey::AGENT_PROXY_DELETE_SANDBOX_FULL;
    case RpcPerfKind::GetStatus:
      return PerfKey::AGENT_PROXY_GET_STATUS_FULL;
  }
  throw std::logic_error("unknown RPC performance key");
}
}  // namespace

class RpcPerf {
 public:
  explicit RpcPerf(RpcPerfKind kind)
      : point_(key_for(kind), SpDiag::global_perf, SpDiag::PerfLevel::SUB_SYSTEM) {
    point_.Start();
  }
  // Exceptional unwinding before the normal completion callback is still a failed sample.
  // Explicit End is essential: the SDK destructor alone would record Abandon.
  ~RpcPerf() {
    finish(pb::ERROR_INTERNAL);
  }
  void finish(int wire_code) noexcept {
    if (ended_.exchange(true, std::memory_order_acq_rel))
      return;
    const int rc = wire_code == pb::ERROR_OK                 ? 0
                   : wire_code == pb::ERROR_CODE_UNSPECIFIED ? pb::ERROR_INTERNAL
                                                             : wire_code;
    try {
      point_.End(rc);
    } catch (...) {
      std::fputs("SpDiag End failed; performance sample may be incomplete\n", stderr);
    }
  }

 private:
  SpDiag::PerfPoint point_;
  std::atomic<bool> ended_{false};
};

void initialize_rpc_perf(const std::string& shm_name) {
  if (shm_name.empty())
    return;
  if (shm_name.rfind("/agent_proxy_", 0) != 0 || shm_name.size() > 128 ||
      shm_name.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
                                 1) != std::string::npos)
    throw std::invalid_argument("spdiag_shm_name must be a dedicated /agent_proxy_* name");
  SpDiag::PerfConfig config;
  config.shmName = shm_name;
  config.programName = SPDIAG_PROGRAM_NAME;
  config.slotCount = static_cast<uint32_t>(PerfKey::COUNT);
  config.levelGate = 1;
  config.dumpIntervalMs = 1000;
  auto& manager = SpDiag::PerfManager::Instance();
  manager.Init(config);
  if (!manager.IsInitialized())
    throw std::runtime_error("SpDiag SHM is unavailable; start the dedicated collection first");
  // Warm SDK timer calibration and all global accumulators on the startup thread.
  // No Start/End: these objects do not contribute samples or Abandon counts.
  for (auto kind :
       {RpcPerfKind::CreateSandbox, RpcPerfKind::RunStep, RpcPerfKind::PauseSandbox,
        RpcPerfKind::ResumeSandbox, RpcPerfKind::DeleteSandbox, RpcPerfKind::GetStatus}) {
    SpDiag::PerfPoint warm(key_for(kind), SpDiag::global_perf, SpDiag::PerfLevel::SUB_SYSTEM);
  }
  enabled = true;
}

void shutdown_rpc_perf() noexcept {
  if (!enabled)
    return;
  enabled = false;
  try {
    SpDiag::PerfManager::Instance().Shutdown();
  } catch (...) {
    std::fputs("SpDiag final flush failed; performance samples may be incomplete\n", stderr);
  }
}

std::shared_ptr<RpcPerf> start_rpc_perf(RpcPerfKind kind) {
  return enabled ? std::make_shared<RpcPerf>(kind) : nullptr;
}
void end_rpc_perf(const std::shared_ptr<RpcPerf>& point, int wire_code) noexcept {
  if (point)
    point->finish(wire_code);
}
RpcPerfCompletion::~RpcPerfCompletion() {
  end_rpc_perf(point_, pb::ERROR_INTERNAL);
}
}  // namespace ap
