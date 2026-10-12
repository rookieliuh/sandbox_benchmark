#include "service/service.h"

#include <brpc/closure_guard.h>
#include <brpc/controller.h>
#include <butil/logging.h>
#include <bvar/bvar.h>

#include <nlohmann/json.hpp>
#include <regex>

#include "adapters/e2b/e2b_adapter.h"
#include "tools/arguments.h"

namespace ap {
namespace {
bvar::Adder<int64_t> calls("agent_proxy_calls"), failures("agent_proxy_failures");
bvar::LatencyRecorder latency("agent_proxy_latency");
Ref reference(const pb::SandboxRef& ref) {
  return {ref.backend(), ref.sandbox_id()};
}
void put_ref(const Ref& in, pb::SandboxRef* out) {
  out->set_backend(in.backend);
  out->set_sandbox_id(in.id);
}
void put_info(const Info& in, pb::SandboxInfo* out) {
  put_ref(in.ref, out->mutable_ref());
  out->set_state(static_cast<pb::SandboxState>(in.state));
  if (!in.template_id.empty())
    out->set_template_id(in.template_id);
  if (!in.expires_at.empty())
    out->set_expires_at(in.expires_at);
  out->set_observed_at(in.observed_at);
  for (const auto& [k, v] : in.metadata)
    (*out->mutable_metadata())[k] = v;
  if (in.envd_ready)
    out->set_envd_ready(*in.envd_ready);
}
void fill(const Result& in, pb::CreateSandboxResponse* out) {
  if (in.info)
    put_info(*in.info, out->mutable_sandbox());
  out->set_completion_basis(static_cast<pb::CompletionBasis>(in.basis));
}
void fill(const Result& in, pb::PauseSandboxResponse* out) {
  if (in.info)
    put_info(*in.info, out->mutable_sandbox());
  out->set_already_paused(in.already);
  out->set_completion_basis(static_cast<pb::CompletionBasis>(in.basis));
}
void fill(const Result& in, pb::ResumeSandboxResponse* out) {
  if (in.info)
    put_info(*in.info, out->mutable_sandbox());
  out->set_already_running(in.already);
  out->set_completion_basis(static_cast<pb::CompletionBasis>(in.basis));
}
void fill(const Result& in, pb::DeleteSandboxResponse* out) {
  out->set_already_absent(in.already);
  out->set_completion_basis(static_cast<pb::CompletionBasis>(in.basis));
}
void fill(const Result& in, pb::GetStatusResponse* out) {
  if (in.info)
    put_info(*in.info, out->mutable_sandbox());
}
void fill(const Result& in, pb::RunStepResponse* out) {
  std::visit(
      [&](const auto& value) {
        using T = std::decay_t<decltype(value)>;
        if constexpr (std::is_same_v<T, BashResult>) {
          auto* r = out->mutable_bash();
          r->set_stdout(value.out);
          r->set_stderr(value.err);
          if (value.exit_code)
            r->set_exit_code(*value.exit_code);
          if (value.pid)
            r->set_pid(*value.pid);
          r->set_termination(static_cast<pb::Termination>(value.termination));
          r->set_stdout_truncated(value.out_truncated);
          r->set_stderr_truncated(value.err_truncated);
          r->set_termination_confirmed(value.termination_confirmed);
          r->set_effective_execution_timeout_ms(value.effective_timeout_ms);
        } else if constexpr (std::is_same_v<T, ReadResult>) {
          out->mutable_read()->set_content(value.content);
          out->mutable_read()->set_size_bytes(value.content.size());
        } else if constexpr (std::is_same_v<T, WriteResult>)
          out->mutable_write()->set_bytes_written(value.bytes);
        else if constexpr (std::is_same_v<T, EditResult>) {
          out->mutable_edit()->set_bytes_written(value.bytes);
          out->mutable_edit()->set_replacement_count(value.replacements);
        }
      },
      in.tool);
}
void complete(const OperationContext& ctx, const std::string& id, const std::optional<Error>& error,
              pb::ResponseContext* out) {
  out->set_request_id(id);
  out->set_outcome(static_cast<pb::Outcome>(ctx.outcome));
  out->set_backend_request_count(ctx.timing.requests);
  auto* s = out->mutable_status();
  s->set_code(pb::OK);
  s->set_source(pb::PROXY);
  s->set_retry_advice(pb::DO_NOT_RETRY);
  if (error) {
    s->set_code(static_cast<pb::ErrorCode>(error->code));
    s->set_source(static_cast<pb::ErrorSource>(error->source));
    s->set_message(error->what());
    s->set_resource_type(error->resource);
    if (error->http_status)
      s->set_http_status(error->http_status);
    if (!error->backend_code.empty())
      s->set_backend_code(error->backend_code);
    if (error->retry_after_ms)
      s->set_retry_after_ms(*error->retry_after_ms);
    if (ctx.outcome == Outcome::Unknown)
      s->set_retry_advice(pb::RECONCILE_FIRST);
    else if (ctx.outcome == Outcome::NotStarted &&
             (error->code == Code::SandboxBusy || error->code == Code::ResourceExhausted ||
              error->code == Code::BackendUnavailable || error->code == Code::RateLimited))
      s->set_retry_advice(pb::SAFE_TO_RETRY);
    failures << 1;
  }
  auto* t = out->mutable_timing();
  auto total = micros(ctx.start, Clock::now());
  t->set_proxy_total_us(total);
  t->set_queue_us(ctx.timing.queue_us);
  t->set_preflight_us(ctx.timing.preflight_us);
  t->set_operation_us(ctx.timing.operation_us);
  t->set_readiness_us(ctx.timing.readiness_us);
  t->set_cleanup_us(ctx.timing.cleanup_us);
  t->set_backend_total_us(ctx.timing.backend_total_us);
  latency << total;
  calls << 1;
  // Only identifiers and numerical diagnostics; no commands, content, credentials or backend body.
  LOG(INFO) << nlohmann::json{{"request_id", id},
                              {"code", static_cast<int>(s->code())},
                              {"outcome", static_cast<int>(ctx.outcome)},
                              {"total_us", total},
                              {"backend_requests", ctx.timing.requests}}
                   .dump(-1, ' ', false, nlohmann::json::error_handler_t::replace);
}
void ttl(uint32_t value) {
  if (value == 0 || value > INT32_MAX)
    throw Error(Code::InvalidArgument, "ttl_seconds must be in 1..INT32_MAX");
}
void environment(const std::map<std::string, std::string>& values) {
  for (const auto& [key, val] : values)
    if (!std::regex_match(key, std::regex("[A-Za-z_][A-Za-z0-9_]*")) ||
        val.find('\0') != std::string::npos)
      throw Error(Code::InvalidArgument, "invalid environment variable");
}
}  // namespace
Service::Service(const std::map<std::string, BackendConfig>& configs, size_t control_threads,
                 size_t step_threads, size_t queue_size)
    : control_pool_(control_threads, queue_size), step_pool_(step_threads, queue_size) {
  for (const auto& [name, config] : configs)
    adapters_[name] = std::make_unique<E2BAdapter>(config);
}
template <class Request, class Response>
void Service::dispatch(google::protobuf::RpcController* controller, const Request* req,
                       Response* resp, google::protobuf::Closure* done, const std::string& backend,
                       const Ref* ref, bool exclusive, bool step, uint32_t default_ms,
                       uint32_t max_ms, Operation operation) {
  brpc::ClosureGuard guard(done);
  auto ctx = std::make_shared<OperationContext>();
  ctx->start = Clock::now();
  auto* cntl = static_cast<brpc::Controller*>(controller);
  ctx->cancelled = [cntl] { return cntl->IsCanceled(); };
  const auto id = req->context().request_id();
  try {
    validate_identifier(id, "request_id");
    validate_identifier(backend, "backend");
    if (req->ByteSizeLong() > kRpcLimit)
      throw Error(Code::PayloadTooLarge, "RPC request too large");
    auto timeout = req->context().has_timeout_ms() ? req->context().timeout_ms() : default_ms;
    if (timeout == 0 || timeout > max_ms)
      throw Error(Code::InvalidArgument, "timeout_ms outside RPC limit");
    ctx->deadline = ctx->start + std::chrono::milliseconds(timeout);
    if (ref)
      validate_ref(*ref);
    auto it = adapters_.find(backend);
    if (it == adapters_.end())
      throw Error(Code::Unsupported, "backend is not configured");
    auto* adapter = it->second.get();
    std::shared_ptr<SandboxLocks::Lease> lease;
    if (exclusive && ref) {
      lease = locks_.acquire(ref->backend + "\n" + ref->id);
      if (!lease)
        throw Error(Code::SandboxBusy, "sandbox already has an active operation");
    }
    auto work = [ctx, adapter, operation = std::move(operation), lease = std::move(lease), id, resp,
                 done, cntl]() mutable {
      brpc::ClosureGuard completion(done);
      Result result;
      std::optional<Error> error;
      ctx->timing.queue_us = micros(ctx->start, Clock::now());
      try {
        ctx->check();
        operation(*adapter, *ctx, result);
      } catch (const Error& e) {
        error = e;
      } catch (const nlohmann::json::exception&) {
        error = Error(Code::BackendProtocolError, "invalid backend field type");
      } catch (const std::exception&) {
        error = Error(Code::InternalError, "internal operation failure");
      } catch (...) {
        error = Error(Code::InternalError, "unexpected internal failure");
      }
      try {
        fill(result, resp);
        complete(*ctx, id, error, resp->mutable_context());
      } catch (...) {
        cntl->SetFailed("response construction failed");
      }
      lease.reset();  // Release before transmitting a response; the next step may arrive
                      // immediately.
    };
    if (!(step ? step_pool_ : control_pool_).submit(std::move(work), ctx->deadline))
      throw Error(Code::ResourceExhausted, "proxy work queue is full");
    guard.release();
  } catch (const Error& e) {
    complete(*ctx, id, e, resp->mutable_context());
  } catch (const std::exception&) {
    complete(*ctx, id, Error(Code::InternalError, "request dispatch failed"),
             resp->mutable_context());
  }
}
void Service::CreateSandbox(google::protobuf::RpcController* c, const pb::CreateSandboxRequest* q,
                            pb::CreateSandboxResponse* r, google::protobuf::Closure* d) {
  dispatch(c, q, r, d, q->backend(), nullptr, false, false, 60000, 300000,
           [q](auto& a, auto& ctx, auto& out) {
             validate_identifier(q->template_id(), "template_id");
             ttl(q->ttl_seconds());
             CreateSpec spec;
             spec.backend = q->backend();
             spec.template_id = q->template_id();
             spec.ttl_seconds = q->ttl_seconds();
             spec.wait_ready = q->wait_ready();
             for (const auto& p : q->env_vars())
               spec.env[p.first] = p.second;
             for (const auto& p : q->metadata())
               spec.metadata[p.first] = p.second;
             environment(spec.env);
             a.create(spec, ctx, out);
           });
}
void Service::RunStep(google::protobuf::RpcController* c, const pb::RunStepRequest* q,
                      pb::RunStepResponse* r, google::protobuf::Closure* d) {
  auto ref = reference(q->ref());
  const bool bash = q->tool() == "bash";
  dispatch(c, q, r, d, ref.backend, &ref, true, true, bash ? 65000 : 30000, bash ? 900000 : 120000,
           [q, ref](auto& a, auto& ctx, auto& out) {
             auto tool = parse_tool(q->tool(), q->arguments_json(),
                                    q->has_execution_timeout_ms()
                                        ? std::optional<uint32_t>(q->execution_timeout_ms())
                                        : std::nullopt);
             a.run(ref, tool, ctx, out);
           });
}
void Service::PauseSandbox(google::protobuf::RpcController* c, const pb::PauseSandboxRequest* q,
                           pb::PauseSandboxResponse* r, google::protobuf::Closure* d) {
  auto ref = reference(q->ref());
  dispatch(c, q, r, d, ref.backend, &ref, true, false, 60000, 300000,
           [ref](auto& a, auto& ctx, auto& out) { a.pause(ref, ctx, out); });
}
void Service::ResumeSandbox(google::protobuf::RpcController* c, const pb::ResumeSandboxRequest* q,
                            pb::ResumeSandboxResponse* r, google::protobuf::Closure* d) {
  auto ref = reference(q->ref());
  dispatch(c, q, r, d, ref.backend, &ref, true, false, 60000, 300000,
           [q, ref](auto& a, auto& ctx, auto& out) {
             ttl(q->ttl_seconds());
             a.resume({ref, q->ttl_seconds(), q->wait_ready()}, ctx, out);
           });
}
void Service::DeleteSandbox(google::protobuf::RpcController* c, const pb::DeleteSandboxRequest* q,
                            pb::DeleteSandboxResponse* r, google::protobuf::Closure* d) {
  auto ref = reference(q->ref());
  put_ref(ref, r->mutable_ref());
  dispatch(
      c, q, r, d, ref.backend, &ref, true, false, 30000, 120000,
      [q, ref](auto& a, auto& ctx, auto& out) { a.remove(ref, q->ignore_missing(), ctx, out); });
}
void Service::GetStatus(google::protobuf::RpcController* c, const pb::GetStatusRequest* q,
                        pb::GetStatusResponse* r, google::protobuf::Closure* d) {
  auto ref = reference(q->ref());
  dispatch(c, q, r, d, ref.backend, &ref, false, false, 10000, 30000,
           [ref](auto& a, auto& ctx, auto& out) { a.status(ref, ctx, out); });
}
}  // namespace ap
