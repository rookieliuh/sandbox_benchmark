#include <brpc/channel.h>
#include <brpc/controller.h>
#include <gflags/gflags.h>
#include <google/protobuf/util/json_util.h>

#include <fstream>
#include <iostream>
#include <memory>
#include <sstream>

#include "agent_proxy.pb.h"

DEFINE_string(server, "127.0.0.1:8080", "Proxy bRPC address");
DEFINE_string(method, "GetStatus", "One of the six RPC method names");
DEFINE_string(request, "", "Protobuf JSON request file, or - for stdin");
DEFINE_int32(rpc_timeout_ms, 0, "Client transport timeout; 0 uses request budget + 2000ms");

int main(int argc, char** argv) {
  gflags::ParseCommandLineFlags(&argc, &argv, true);
  auto* method =
      benchmark::agent::v1::AgentProxyService::descriptor()->FindMethodByName(FLAGS_method);
  if (!method || FLAGS_request.empty()) {
    std::cerr << "valid --method and --request required\n";
    return 1;
  }
  std::ifstream file;
  std::istream* input = &std::cin;
  if (FLAGS_request != "-") {
    file.open(FLAGS_request);
    if (!file) {
      std::cerr << "cannot read request\n";
      return 1;
    }
    input = &file;
  }
  std::string json;
  char chunk[8192];
  while (input->read(chunk, sizeof(chunk)) || input->gcount()) {
    json.append(chunk, static_cast<size_t>(input->gcount()));
    if (json.size() > 16 * 1024 * 1024) {
      std::cerr << "request too large\n";
      return 1;
    }
  }
  auto* factory = google::protobuf::MessageFactory::generated_factory();
  std::unique_ptr<google::protobuf::Message> request(
      factory->GetPrototype(method->input_type())->New());
  std::unique_ptr<google::protobuf::Message> response(
      factory->GetPrototype(method->output_type())->New());
  auto parsed = google::protobuf::util::JsonStringToMessage(json, request.get());
  if (!parsed.ok()) {
    std::cerr << "invalid request JSON\n";
    return 1;
  }
  auto* timeout_field = request->GetDescriptor()->FindFieldByName("timeout_ms");
  int budget = 10000;
  if (timeout_field) {
    auto ms = request->GetReflection()->GetUInt32(*request, timeout_field);
    if (ms > 900000) {
      std::cerr << "timeout too large\n";
      return 1;
    }
    budget = static_cast<int>(ms);
  }
  if (FLAGS_method == "RunStep") {
    auto* tool = request->GetDescriptor()->FindFieldByName("tool");
    if (request->GetReflection()->GetString(*request, tool) == "bash")
      budget += 7000;
  }
  brpc::Channel channel;
  brpc::ChannelOptions options;
  options.protocol = "baidu_std";
  options.max_retry = 0;
  options.connect_timeout_ms = 3000;
  options.timeout_ms = FLAGS_rpc_timeout_ms > 0 ? FLAGS_rpc_timeout_ms : budget + 2000;
  if (channel.Init(FLAGS_server.c_str(), &options) != 0) {
    std::cerr << "channel initialization failed\n";
    return 1;
  }
  brpc::Controller controller;
  controller.set_max_retry(0);
  channel.CallMethod(method, &controller, request.get(), response.get(), nullptr);
  if (controller.Failed()) {
    std::cerr << "RPC transport failure, code=" << controller.ErrorCode()
              << "; operation outcome may be unknown\n";
    return 3;
  }
  google::protobuf::util::JsonPrintOptions print;
  print.preserve_proto_field_names = true;
  print.add_whitespace = true;
  print.always_print_primitive_fields = true;
  std::string output;
  if (!google::protobuf::util::MessageToJsonString(*response, &output, print).ok()) {
    std::cerr << "response serialization failed\n";
    return 1;
  }
  std::cout << output << '\n';
  const auto* field = response->GetDescriptor()->FindFieldByName("error");
  const auto& status = response->GetReflection()->GetMessage(*response, field);
  const auto code =
      status.GetReflection()->GetEnumValue(status, status.GetDescriptor()->FindFieldByName("code"));
  auto* success = response->GetDescriptor()->FindFieldByName("success");
  return code == benchmark::agent::v1::ERROR_OK &&
                 response->GetReflection()->GetBool(*response, success)
             ? 0
             : 2;
}
