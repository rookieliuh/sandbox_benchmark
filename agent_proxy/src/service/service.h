#pragma once
#include "adapters/sandbox_adapter.h"
#include "agent_proxy.pb.h"
#include "config/config.h"
#include "core/executor.h"
#include "observability/rpc_perf.h"
namespace ap {
namespace pb = benchmark::agent::v1;
class Service final : public pb::AgentProxyService {
 public:
  Service(const std::map<std::string, BackendConfig>&, size_t control_threads, size_t step_threads,
          size_t queue_size, size_t max_records = 10000,
          size_t max_record_bytes = 256 * 1024 * 1024);
  void CreateSandbox(google::protobuf::RpcController*, const pb::CreateSandboxRequest*,
                     pb::CreateSandboxResponse*, google::protobuf::Closure*) override;
  void RunStep(google::protobuf::RpcController*, const pb::RunStepRequest*, pb::RunStepResponse*,
               google::protobuf::Closure*) override;
  void PauseSandbox(google::protobuf::RpcController*, const pb::PauseSandboxRequest*,
                    pb::PauseSandboxResponse*, google::protobuf::Closure*) override;
  void ResumeSandbox(google::protobuf::RpcController*, const pb::ResumeSandboxRequest*,
                     pb::ResumeSandboxResponse*, google::protobuf::Closure*) override;
  void DeleteSandbox(google::protobuf::RpcController*, const pb::DeleteSandboxRequest*,
                     pb::DeleteSandboxResponse*, google::protobuf::Closure*) override;
  void GetStatus(google::protobuf::RpcController*, const pb::GetStatusRequest*,
                 pb::GetStatusResponse*, google::protobuf::Closure*) override;

 private:
  struct Entry {
    std::string fingerprint;
    pb::OperationRecord record;
    bool create_absent = false;
    size_t bytes = 0;
  };
  struct Binding {
    Ref ref;
    pb::SandboxInfo info;
  };
  struct WorkResult {
    Result result;
    std::string sandbox_id;
    bool cleanup_confirmed = false;
    std::shared_ptr<SandboxLocks::Lease> lease;
  };
  using Operation = std::function<void(OperationContext&, WorkResult&)>;
  template <class Request, class Response>
  void dispatch(google::protobuf::RpcController*, const Request*, Response*,
                google::protobuf::Closure*, RpcPerfKind, pb::OperationKind, bool step, uint32_t timeout_ms,
                uint32_t max_ms, Operation);
  template <class Response>
  void finish(OperationContext&, const pb::RequestMeta&, Response*, WorkResult&,
              const std::optional<Error>&, const std::shared_ptr<Entry>&);
  Binding lookup(const std::string& id);
  SandboxAdapter& adapter(const std::string& backend);
  void acquire(WorkResult&, const std::string& id);
  std::string bind(const Info&, const TemplateConfig&, uint32_t ttl);
  void update(const std::string&, const Info&);
  std::map<std::string, BackendConfig> configs_;
  std::map<std::string, std::unique_ptr<SandboxAdapter>> adapters_;
  std::mutex mutex_;
  std::map<std::string, std::shared_ptr<Entry>> records_;
  std::map<std::string, Binding> sandboxes_;
  size_t max_records_, max_record_bytes_, record_bytes_ = 0;
  uint64_t next_sandbox_ = 0;
  std::string instance_id_;
  SandboxLocks locks_;
  // Pools must be destroyed before the state captured by their jobs.
  Executor control_pool_, step_pool_;
};
}  // namespace ap
