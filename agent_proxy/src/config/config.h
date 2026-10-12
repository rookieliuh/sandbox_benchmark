#pragma once
#include <cstdint>
#include <map>
#include <string>
namespace ap {
struct TemplateConfig {
  std::string backend_template_id;
  uint32_t cpu_count = 0;
  uint64_t memory_mib = 0;
};
struct BackendConfig {
  std::string name, api_url, api_key, profile = "kasandbox";
  std::string envd_url_pattern, sandbox_domain, envd_scheme = "https", user;
  std::map<std::string, TemplateConfig> templates;
};
std::map<std::string, BackendConfig> load_backends(const std::string& path);
}  // namespace ap
