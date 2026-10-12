#pragma once
#include <map>
#include <string>
namespace ap {
struct BackendConfig {
  std::string name, api_url, api_key, profile = "kasandbox";
  std::string envd_url_pattern, sandbox_domain, envd_scheme = "https", user;
};
std::map<std::string, BackendConfig> load_backends(const std::string& path);
}  // namespace ap
