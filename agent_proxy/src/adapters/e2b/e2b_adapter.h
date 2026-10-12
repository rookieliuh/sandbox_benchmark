#pragma once
#include <nlohmann/json.hpp>

#include "adapters/sandbox_adapter.h"
#include "config/config.h"
#include "transport/http.h"
namespace ap {
class E2BAdapter final : public SandboxAdapter {
 public:
  explicit E2BAdapter(BackendConfig config) : config_(std::move(config)) {}
  void create(const CreateSpec&, OperationContext&, Result&) override;
  void run(const Ref&, const Tool&, OperationContext&, Result&) override;
  void pause(const Ref&, OperationContext&, Result&) override;
  void resume(const ResumeSpec&, OperationContext&, Result&) override;
  void remove(const Ref&, bool, OperationContext&, Result&) override;
  void status(const Ref&, OperationContext&, Result&) override;

 private:
  struct Connection {
    Info info;
    std::string url, token, user;
    bool octet = false;
  };
  BackendConfig config_;
  HttpClient http_;
  HttpRequest control(const std::string&, const std::string&, Phase, bool = false) const;
  HttpRequest envd(const Connection&, const std::string&, const std::string&, Phase, bool = false,
                   bool rpc = false) const;
  nlohmann::json control_json(HttpRequest&, OperationContext&) const;
  Info info(const nlohmann::json&, const Ref&, State fallback = State::Unknown) const;
  Connection connection(const nlohmann::json&, const Ref&) const;
  nlohmann::json details(const Ref&, OperationContext&, Phase) const;
  nlohmann::json unary(const Connection&, const std::string&, const nlohmann::json&,
                       OperationContext&, Phase, bool = false) const;
  void ready(const Connection&, OperationContext&, Result&) const;
  std::string read_file(const Connection&, const std::string&, OperationContext&) const;
  void write_file(const Connection&, const std::string&, const std::string&,
                  OperationContext&) const;
  void bash(const Connection&, const Bash&, OperationContext&, Result&) const;
};
}  // namespace ap
