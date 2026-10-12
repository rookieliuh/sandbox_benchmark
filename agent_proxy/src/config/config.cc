#include "config/config.h"

#include <cstdlib>
#include <fstream>
#include <nlohmann/json.hpp>
#include <regex>
#include <set>
#include <stdexcept>
namespace ap {
std::map<std::string, BackendConfig> load_backends(const std::string& path) {
  std::ifstream input(path);
  if (!input)
    throw std::runtime_error("cannot read backend configuration");
  auto root = nlohmann::json::parse(input);
  if (!root.is_object() || !root.contains("backends") || !root["backends"].is_object())
    throw std::runtime_error("configuration requires backends object");
  std::map<std::string, BackendConfig> configs;
  const std::set<std::string> fields = {
      "type",           "profile",     "api_url", "api_key_env", "envd_url_pattern",
      "sandbox_domain", "envd_scheme", "user",    "templates"};
  for (auto it = root["backends"].begin(); it != root["backends"].end(); ++it) {
    const auto& j = it.value();
    if (!std::regex_match(it.key(), std::regex("[A-Za-z0-9_-]{1,64}")) || !j.is_object())
      throw std::runtime_error("invalid backend definition");
    for (auto field = j.begin(); field != j.end(); ++field)
      if (!fields.count(field.key()))
        throw std::runtime_error("unknown backend configuration field");
    if (j.value("type", "") != "e2b")
      throw std::runtime_error("only e2b adapters are implemented");
    BackendConfig c;
    c.name = it.key();
    c.api_url = j.at("api_url").get<std::string>();
    if (c.api_url.empty())
      throw std::runtime_error("api_url must not be empty");
    c.profile = j.value("profile", "kasandbox");
    if (c.profile != "kasandbox" && c.profile != "e2b_cloud")
      throw std::runtime_error("invalid E2B profile");
    c.envd_url_pattern = j.value("envd_url_pattern", "");
    c.sandbox_domain = j.value("sandbox_domain", "");
    c.envd_scheme = j.value("envd_scheme", "https");
    c.user = j.value("user", "");
    if (j.contains("templates")) {
      if (!j["templates"].is_object())
        throw std::runtime_error("templates must be an object");
      for (auto t = j["templates"].begin(); t != j["templates"].end(); ++t) {
        const auto& v = t.value();
        if (t.key().empty() || !v.is_object() || v.size() != 3 ||
            !v.contains("backend_template_id") || !v["backend_template_id"].is_string() ||
            !v.contains("cpu_count") || !v["cpu_count"].is_number_unsigned() ||
            !v.contains("memory_mib") || !v["memory_mib"].is_number_unsigned())
          throw std::runtime_error("template requires backend_template_id, cpu_count, memory_mib");
        auto cpu = v["cpu_count"].get<uint64_t>();
        auto mem = v["memory_mib"].get<uint64_t>();
        auto id = v["backend_template_id"].get<std::string>();
        if (cpu == 0 || cpu > UINT32_MAX || mem == 0 || id.empty())
          throw std::runtime_error("invalid template specification");
        c.templates.emplace(t.key(), TemplateConfig{id, static_cast<uint32_t>(cpu), mem});
      }
    }
    if (c.envd_scheme != "https" && c.envd_scheme != "http")
      throw std::runtime_error("invalid envd scheme");
    for (const auto* url : {&c.api_url, &c.envd_url_pattern}) {
      if (!url->empty() && (!std::regex_match(*url, std::regex("https?://[^\\s]+")) ||
                            url->find('@') != std::string::npos))
        throw std::runtime_error("backend URL must use HTTP(S) without embedded credentials");
    }
    while (!c.api_url.empty() && c.api_url.back() == '/')
      c.api_url.pop_back();
    const auto variable = j.at("api_key_env").get<std::string>();
    const auto* key = std::getenv(variable.c_str());
    if (!key || !*key)
      throw std::runtime_error("backend API key environment variable is missing");
    c.api_key = key;
    if (c.api_key.find_first_of("\r\n") != std::string::npos)
      throw std::runtime_error("invalid API key");
    configs.emplace(c.name, std::move(c));
  }
  if (configs.empty())
    throw std::runtime_error("at least one backend is required");
  return configs;
}
}  // namespace ap
