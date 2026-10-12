#pragma once
#include "core/model.h"
namespace ap {
Tool parse_tool(const std::string& name, const std::string& json,
                std::optional<uint32_t> execution_timeout);
void validate_ref(const Ref&);
void validate_identifier(const std::string&, const char* field);
void validate_text(const std::string& text);
std::string replace_once(const std::string& content, const Edit& edit);
}  // namespace ap
