#include "adapters/e2b/connect_codec.h"

#include <openssl/evp.h>

#include <limits>

#include "transport/http.h"
namespace ap {
std::string base64_encode(const std::string& in) {
  if (in.size() > kRpcLimit)
    throw Error(Code::PayloadTooLarge, "base64 input too large");
  std::string out(4 * ((in.size() + 2) / 3) + 1, '\0');
  auto n = EVP_EncodeBlock(reinterpret_cast<unsigned char*>(out.data()),
                           reinterpret_cast<const unsigned char*>(in.data()),
                           static_cast<int>(in.size()));
  out.resize(n);
  return out;
}
std::string base64_decode(const std::string& input) {
  if (input.empty())
    return {};
  if (input.size() > kRpcLimit)
    throw Error(Code::BackendProtocolError, "base64 payload too large", Source::Envd);
  auto in = input;
  for (auto& c : in) {
    if (c == '-')
      c = '+';
    else if (c == '_')
      c = '/';
  }
  while (in.size() % 4)
    in += '=';
  size_t pad = in.back() == '=' ? 1 : 0;
  if (in.size() > 1 && in[in.size() - 2] == '=')
    ++pad;
  for (size_t i = 0; i < in.size() - pad; ++i)
    if (!((in[i] >= 'a' && in[i] <= 'z') || (in[i] >= 'A' && in[i] <= 'Z') ||
          (in[i] >= '0' && in[i] <= '9') || in[i] == '+' || in[i] == '/'))
      throw Error(Code::BackendProtocolError, "invalid base64 output", Source::Envd);
  std::string out(3 * (in.size() / 4), '\0');
  auto n = EVP_DecodeBlock(reinterpret_cast<unsigned char*>(out.data()),
                           reinterpret_cast<const unsigned char*>(in.data()),
                           static_cast<int>(in.size()));
  if (n < 0 || static_cast<size_t>(n) < pad)
    throw Error(Code::BackendProtocolError, "invalid base64 output", Source::Envd);
  out.resize(n - pad);
  return out;
}
std::string connect_frame(const nlohmann::json& body, uint8_t flags) {
  auto data = body.dump();
  if (data.size() > kRpcLimit)
    throw Error(Code::PayloadTooLarge, "Connect frame too large");
  std::string out(5, '\0');
  out[0] = static_cast<char>(flags);
  uint32_t n = static_cast<uint32_t>(data.size());
  for (int i = 0; i < 4; ++i)
    out[1 + i] = static_cast<char>((n >> (24 - 8 * i)) & 255);
  return out + data;
}
void ConnectDecoder::feed(const char* bytes, size_t size) {
  if (ended_ && size)
    throw Error(Code::BackendProtocolError, "data after Connect trailer", Source::Envd);
  buffer_.append(bytes, size);
  while (buffer_.size() >= 5) {
    uint8_t flags = static_cast<uint8_t>(buffer_[0]);
    uint32_t len = 0;
    for (int i = 1; i <= 4; ++i)
      len = (len << 8) | static_cast<uint8_t>(buffer_[i]);
    if (flags != 0 && flags != 2)
      throw Error(Code::BackendProtocolError, "unsupported Connect frame flags", Source::Envd);
    if (len > kRpcLimit)
      throw Error(Code::BackendProtocolError, "Connect frame too large", Source::Envd);
    if (buffer_.size() < 5ULL + len)
      return;
    nlohmann::json j;
    try {
      j = nlohmann::json::parse(buffer_.substr(5, len));
    } catch (const nlohmann::json::exception&) {
      throw Error(Code::BackendProtocolError, "invalid Connect JSON", Source::Envd);
    }
    buffer_.erase(0, 5ULL + len);
    if (!j.is_object())
      throw Error(Code::BackendProtocolError, "Connect frame must be an object", Source::Envd);
    if (flags == 2) {
      ended_ = true;
      if (!buffer_.empty())
        throw Error(Code::BackendProtocolError, "data after Connect trailer", Source::Envd);
      if (j.contains("error")) {
        if (!j["error"].is_object() || !j["error"].contains("code") ||
            !j["error"]["code"].is_string())
          throw Error(Code::BackendProtocolError, "malformed Connect error", Source::Envd);
        throw connect_error(j["error"]["code"].get<std::string>());
      }
    } else {
      try {
        event_(j);
      } catch (const nlohmann::json::exception&) {
        throw Error(Code::BackendProtocolError, "invalid process event fields", Source::Envd);
      }
    }
  }
}
void ConnectDecoder::finish() const {
  if (!ended_ || !buffer_.empty())
    throw Error(Code::BackendProtocolError, "incomplete Connect stream", Source::Envd);
}
}  // namespace ap
