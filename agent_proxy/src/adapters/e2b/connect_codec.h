#pragma once
#include <nlohmann/json.hpp>

#include "core/model.h"
namespace ap {
std::string connect_frame(const nlohmann::json& body, uint8_t flags = 0);
std::string base64_decode(const std::string& input);
std::string base64_encode(const std::string& input);
class ConnectDecoder {
 public:
  explicit ConnectDecoder(std::function<void(const nlohmann::json&)> event)
      : event_(std::move(event)) {}
  void feed(const char* bytes, size_t size);
  void finish() const;

 private:
  std::function<void(const nlohmann::json&)> event_;
  std::string buffer_;
  bool ended_ = false;
};
}  // namespace ap
