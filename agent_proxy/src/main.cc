#include <brpc/server.h>
#include <curl/curl.h>
#include <gflags/gflags.h>

#include <iostream>

#include "config/config.h"
#include "service/service.h"

DEFINE_string(listen, "127.0.0.1:8080", "bRPC listen address");
DEFINE_string(backends, "configs/mock.json", "Backend configuration JSON");
DEFINE_int32(control_threads, 4, "Control API worker threads");
DEFINE_int32(step_threads, 16, "RunStep worker threads");
DEFINE_int32(queue_size, 128, "Maximum waiting requests per worker pool");
DEFINE_int32(admin_port, 0, "Optional bRPC internal services port; 0 disables builtins");
int main(int argc, char** argv) {
  gflags::ParseCommandLineFlags(&argc, &argv, true);
  if (FLAGS_control_threads <= 0 || FLAGS_step_threads <= 0 || FLAGS_queue_size <= 0) {
    std::cerr << "worker and queue limits must be positive\n";
    return 1;
  }
  if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) {
    std::cerr << "curl initialization failed\n";
    return 1;
  }
  int result = 0;
  try {
    auto configs = ap::load_backends(FLAGS_backends);
    ap::Service service(configs, FLAGS_control_threads, FLAGS_step_threads, FLAGS_queue_size);
    brpc::Server server;
    if (server.AddService(&service, brpc::SERVER_DOESNT_OWN_SERVICE) != 0)
      throw std::runtime_error("cannot register service");
    gflags::SetCommandLineOption("max_body_size", "16777216");
    brpc::ServerOptions options;
    options.has_builtin_services = FLAGS_admin_port != 0;
    if (FLAGS_admin_port)
      options.internal_port = FLAGS_admin_port;
    if (server.Start(FLAGS_listen.c_str(), &options) != 0)
      throw std::runtime_error("cannot start server");
    server.RunUntilAskedToQuit();  // Stops accepting work and joins in-flight RPCs before pool
                                   // destruction.
  } catch (const std::exception&) {
    std::cerr << "startup/shutdown failed; check configuration, dependencies and listen address\n";
    result = 1;
  }
  curl_global_cleanup();
  return result;
}
