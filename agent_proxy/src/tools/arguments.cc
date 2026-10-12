#include "tools/arguments.h"

#include <nlohmann/json.hpp>
#include <regex>
#include <set>
namespace ap {
using Json = nlohmann::json;
void validate_identifier(const std::string& value, const char* field) {
  if (value.empty() || value.size() > 256 || value.find('\0') != std::string::npos ||
      value.find_first_of("\r\n") != std::string::npos)
    throw Error(Code::InvalidArgument, std::string("invalid ") + field);
}
void validate_ref(const Ref& ref) {
  validate_identifier(ref.backend, "backend");
  validate_identifier(ref.id, "sandbox_id");
  if (!std::regex_match(ref.id, std::regex("[A-Za-z0-9_-]+")))
    throw Error(Code::InvalidArgument, "sandbox_id contains unsupported characters");
}
void validate_text(const std::string& text) {
  // The JSON serializer performs strict UTF-8 validation, including surrogate sequences.
  try {
    (void)Json(text).dump();
  } catch (const Json::exception&) {
    throw Error(Code::Unsupported, "file is not valid UTF-8", Source::Envd, "file");
  }
}
static std::string string_field(const Json& j, const char* name, bool required = true) {
  if (!j.contains(name)) {
    if (required)
      throw Error(Code::InvalidArgument, std::string("missing field: ") + name);
    return {};
  }
  if (!j.at(name).is_string())
    throw Error(Code::InvalidArgument, std::string("field must be a string: ") + name);
  return j.at(name).get<std::string>();
}
static void path_check(const std::string& path) {
  if (path.empty() || path.front() != '/' || path.size() > 4096 ||
      path.find('\0') != std::string::npos)
    throw Error(Code::InvalidArgument, "path must be an absolute sandbox path without NUL");
}
Tool parse_tool(const std::string& name, const std::string& body, std::optional<uint32_t> timeout) {
  if (body.size() > kRpcLimit)
    throw Error(Code::PayloadTooLarge, "arguments_json too large");
  std::set<std::string> allowed;
  if (name == "bash")
    allowed = {"command", "cwd", "env"};
  else if (name == "read")
    allowed = {"path"};
  else if (name == "write")
    allowed = {"path", "content"};
  else if (name == "edit")
    allowed = {"path", "oldString", "newString"};
  else
    throw Error(Code::Unsupported, "unsupported tool");
  if (name != "bash" && timeout)
    throw Error(Code::InvalidArgument, "execution_timeout_ms is only valid for bash");
  Json j;
  try {
    j = Json::parse(body);
  } catch (const Json::exception&) {
    throw Error(Code::InvalidArgument, "invalid arguments_json");
  }
  if (!j.is_object())
    throw Error(Code::InvalidArgument, "arguments_json must be an object");
  for (auto it = j.begin(); it != j.end(); ++it)
    if (!allowed.count(it.key()))
      throw Error(Code::InvalidArgument, "unknown tool argument");
  if (name == "bash") {
    Bash b;
    b.command = string_field(j, "command");
    b.cwd = string_field(j, "cwd", false);
    if (b.command.empty() || b.command.find('\0') != std::string::npos)
      throw Error(Code::InvalidArgument, "command must be nonempty and contain no NUL");
    if (b.command.size() > kOutputLimit)
      throw Error(Code::PayloadTooLarge, "command too large");
    if (j.contains("cwd"))
      path_check(b.cwd);
    if (j.contains("env")) {
      if (!j["env"].is_object())
        throw Error(Code::InvalidArgument, "env must be a string map");
      for (auto it = j["env"].begin(); it != j["env"].end(); ++it) {
        if (!std::regex_match(it.key(), std::regex("[A-Za-z_][A-Za-z0-9_]*")) ||
            !it.value().is_string())
          throw Error(Code::InvalidArgument, "invalid environment variable");
        auto val = it.value().get<std::string>();
        if (val.find('\0') != std::string::npos)
          throw Error(Code::InvalidArgument, "environment value contains NUL");
        b.env[it.key()] = std::move(val);
      }
    }
    b.timeout_ms = timeout.value_or(60000);
    if (b.timeout_ms == 0 || b.timeout_ms > 900000)
      throw Error(Code::InvalidArgument, "invalid execution_timeout_ms");
    return b;
  }
  auto path = string_field(j, "path");
  path_check(path);
  if (name == "read")
    return Read{path};
  if (name == "write") {
    auto content = string_field(j, "content");
    if (content.size() > kFileLimit)
      throw Error(Code::PayloadTooLarge, "file too large");
    return Write{path, content};
  }
  Edit e{path, string_field(j, "oldString"), string_field(j, "newString")};
  if (e.old_string.empty())
    throw Error(Code::InvalidArgument, "oldString must not be empty");
  if (e.old_string.size() > kFileLimit || e.new_string.size() > kFileLimit)
    throw Error(Code::PayloadTooLarge, "edit argument too large");
  return e;
}
std::string replace_once(const std::string& content, const Edit& e) {
  auto pos = content.find(e.old_string);
  if (pos == std::string::npos || content.find(e.old_string, pos + 1) != std::string::npos)
    throw Error(Code::EditConflict, "oldString must match exactly once", Source::Proxy, "file");
  if (content.size() - e.old_string.size() + e.new_string.size() > kFileLimit)
    throw Error(Code::PayloadTooLarge, "edited file too large");
  auto result = content;
  result.replace(pos, e.old_string.size(), e.new_string);
  return result;
}
}  // namespace ap
