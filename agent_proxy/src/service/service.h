#pragma once
#include "adapters/sandbox_adapter.h"
#include "agent_proxy.pb.h"
#include "config/config.h"
#include "core/executor.h"
namespace ap {
namespace pb = agentproxy::v1;
class Service final : public pb::AgentProxyService {
 public:
  Service(const std::map<std::string, BackendConfig>& configs, size_t control_threads,
          size_t step_threads, size_t queue_size);
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
  using Operation = std::function<void(SandboxAdapter&, OperationContext&, Result&)>;
  template <class Request, class Response>
  void dispatch(google::protobuf::RpcController*, const Request*, Response*,
                google::protobuf::Closure*, const std::string& backend, const Ref* ref,
                bool exclusive, bool step, uint32_t default_ms, uint32_t max_ms, Operation);
  std::map<std::string, std::unique_ptr<SandboxAdapter>> adapters_;
  SandboxLocks locks_;
  Executor control_pool_, step_pool_;
};
}  // namespace ap
