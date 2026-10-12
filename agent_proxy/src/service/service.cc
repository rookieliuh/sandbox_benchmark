#include "service/service.h"

#include <brpc/closure_guard.h>
#include <brpc/controller.h>
#include <butil/logging.h>
#include <bvar/bvar.h>
#include <openssl/rand.h>
#include <openssl/sha.h>

#include <nlohmann/json.hpp>

#include "adapters/e2b/connect_codec.h"
#include "adapters/e2b/e2b_adapter.h"
#include "tools/arguments.h"

namespace ap {
namespace {
using Json = nlohmann::json;
bvar::Adder<int64_t> calls("agent_proxy_calls"), failures("agent_proxy_failures");
bvar::LatencyRecorder latency("agent_proxy_latency");
constexpr size_t kReservation = kRpcLimit + 65536;
uint64_t now_ms() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}
std::string hex(const unsigned char* p, size_t n) {
  std::string out;
  for (size_t i = 0; i < n; ++i) {
    out += "0123456789abcdef"[p[i] >> 4];
    out += "0123456789abcdef"[p[i] & 15];
  }
  return out;
}
std::string fingerprint(const google::protobuf::Message& request) {
  auto data = request.GetTypeName() + "\n" + request.SerializeAsString();
  unsigned char digest[SHA256_DIGEST_LENGTH];
  SHA256(reinterpret_cast<const unsigned char*>(data.data()), data.size(), digest);
  return hex(digest, sizeof(digest));
}
pb::ErrorCode error_code(const Error& e, const Result& result) {
  switch (e.code) {
    case Code::InvalidArgument:
    case Code::PayloadTooLarge:
      return pb::ERROR_INVALID_ARGUMENT;
    case Code::UnsupportedBackend:
      return pb::ERROR_UNSUPPORTED_BACKEND;
    case Code::UnsupportedTool:
      return pb::ERROR_UNSUPPORTED_TOOL;
    case Code::Unsupported:
      return pb::ERROR_UNSUPPORTED_CAPABILITY;
    case Code::NotFound:
      return pb::ERROR_NOT_FOUND;
    case Code::InvalidState:
    case Code::EditConflict:
    case Code::SandboxBusy:
    case Code::ResourceExhausted:
      return pb::ERROR_INVALID_STATE;
    case Code::DuplicateRequest:
      return pb::ERROR_DUPLICATE_REQUEST;
    case Code::RequestIdConflict:
      return pb::ERROR_REQUEST_ID_CONFLICT;
    case Code::StorageUnavailable:
      return pb::ERROR_STORAGE_UNAVAILABLE;
    case Code::StatusUnavailable:
      return pb::ERROR_STATUS_UNAVAILABLE;
    case Code::DeadlineExceeded: {
      auto* bash = std::get_if<BashResult>(&result.tool);
      return (bash && bash->termination == Termination::TimedOut) ? pb::ERROR_TOOL_TIMEOUT
                                                                  : pb::ERROR_OPERATION_TIMEOUT;
    }
    case Code::Cancelled:
      return pb::ERROR_OPERATION_TIMEOUT;
    case Code::InternalError:
      return pb::ERROR_INTERNAL;
    default:
      return pb::ERROR_BACKEND_ERROR;
  }
}
pb::SandboxState state(const Info& info) {
  if (info.state == State::Paused)
    return pb::SANDBOX_PAUSED;
  if (info.state == State::Deleted)
    return pb::SANDBOX_DELETED;
  if (info.state == State::Running && info.envd_ready.value_or(false))
    return pb::SANDBOX_READY;
  return pb::SANDBOX_UNKNOWN;
}
std::string result_json(const ToolResult& result) {
  Json j = nullptr;
  std::visit(
      [&](const auto& value) {
        using T = std::decay_t<decltype(value)>;
        if constexpr (std::is_same_v<T, BashResult>) {
          const char* names[] = {"UNKNOWN", "EXITED", "TIMED_OUT", "CANCELLED", "SIGNALLED"};
          j = {{"tool", "bash"},
               {"stdout", value.out},
               {"stderr", value.err},
               {"stdout_base64", base64_encode(value.out)},
               {"stderr_base64", base64_encode(value.err)},
               {"exit_code", value.exit_code ? Json(*value.exit_code) : Json(nullptr)},
               {"pid", value.pid ? Json(*value.pid) : Json(nullptr)},
               {"termination", names[static_cast<int>(value.termination)]},
               {"stdout_truncated", value.out_truncated},
               {"stderr_truncated", value.err_truncated},
               {"termination_confirmed", value.termination_confirmed},
               {"effective_execution_timeout_ms", value.effective_timeout_ms}};
        } else if constexpr (std::is_same_v<T, ReadResult>) {
          j = {{"tool", "read"}, {"content", value.content}, {"size_bytes", value.content.size()}};
        } else if constexpr (std::is_same_v<T, WriteResult>) {
          j = {{"tool", "write"}, {"bytes_written", value.bytes}};
        } else if constexpr (std::is_same_v<T, EditResult>) {
          j = {{"tool", "edit"},
               {"bytes_written", value.bytes},
               {"replacement_count", value.replacements}};
        }
      },
      result);
  return j.dump(-1, ' ', false, Json::error_handler_t::replace);
}
template <class Response>
void save_response(const Response& response, pb::OperationRecord* record) {
  if constexpr (std::is_same_v<Response, pb::CreateSandboxResponse>)
    *record->mutable_create_result() = response;
  else if constexpr (std::is_same_v<Response, pb::RunStepResponse>)
    *record->mutable_step_result() = response;
  else if constexpr (std::is_same_v<Response, pb::PauseSandboxResponse>)
    *record->mutable_pause_result() = response;
  else if constexpr (std::is_same_v<Response, pb::ResumeSandboxResponse>)
    *record->mutable_resume_result() = response;
  else if constexpr (std::is_same_v<Response, pb::DeleteSandboxResponse>)
    *record->mutable_delete_result() = response;
}
void validate_meta(const pb::RequestMeta& meta) {
  validate_identifier(meta.request_id(), "meta.request_id");
  if (meta.ByteSizeLong() > 8192 || !pb::Phase_IsValid(meta.phase()))
    throw Error(Code::InvalidArgument, "invalid or oversized request metadata");
}
}  // namespace

Service::Service(const std::map<std::string, BackendConfig>& configs, size_t control_threads,
                 size_t step_threads, size_t queue_size, size_t max_records,
                 size_t max_record_bytes)
    : configs_(configs),
      max_records_(max_records),
      max_record_bytes_(max_record_bytes),
      control_pool_(control_threads, queue_size),
      step_pool_(step_threads, queue_size) {
  unsigned char random[16];
  if (RAND_bytes(random, sizeof(random)) != 1)
    throw std::runtime_error("cannot create instance ID");
  instance_id_ = hex(random, sizeof(random));
  for (const auto& [name, config] : configs)
    adapters_[name] = std::make_unique<E2BAdapter>(config);
}
SandboxAdapter& Service::adapter(const std::string& backend) {
  auto it = adapters_.find(backend);
  if (it == adapters_.end())
    throw Error(Code::UnsupportedBackend, "backend is not configured");
  return *it->second;
}
Service::Binding Service::lookup(const std::string& id) {
  validate_identifier(id, "sandbox_id");
  std::lock_guard<std::mutex> lock(mutex_);
  auto it = sandboxes_.find(id);
  if (it == sandboxes_.end())
    throw Error(Code::NotFound, "unknown proxy sandbox ID");
  return it->second;
}
void Service::acquire(WorkResult& out, const std::string& id) {
  out.lease = locks_.acquire(id);
  if (!out.lease)
    throw Error(Code::SandboxBusy, "sandbox already has an active operation");
}
std::string Service::bind(const Info& in, const TemplateConfig& spec, uint32_t ttl) {
  std::lock_guard<std::mutex> lock(mutex_);
  const auto id = "ap-" + instance_id_ + "-" + std::to_string(++next_sandbox_);
  Binding binding;
  binding.ref = in.ref;
  auto& info = binding.info;
  info.set_sandbox_id(id);
  info.set_backend(in.ref.backend);
  info.set_template_(in.template_id.empty() ? spec.backend_template_id : in.template_id);
  info.mutable_effective_spec()->set_cpu_count(spec.cpu_count);
  info.mutable_effective_spec()->set_memory_mib(spec.memory_mib);
  info.set_effective_ttl_seconds(ttl);
  info.set_state(state(in));
  info.set_observed_at_unix_ms(now_ms());
  for (const char* c : {"bash", "read", "write", "edit", "pause", "resume"})
    info.add_capabilities(c);
  sandboxes_.emplace(id, std::move(binding));
  return id;
}
void Service::update(const std::string& id, const Info& in) {
  std::lock_guard<std::mutex> lock(mutex_);
  auto& info = sandboxes_.at(id).info;
  if (info.state() == pb::SANDBOX_DELETED)
    return;
  info.set_state(state(in));
  info.set_observed_at_unix_ms(now_ms());
}

template <class Response>
void Service::finish(OperationContext& ctx, const pb::RequestMeta& meta, Response* resp,
                     WorkResult& out, const std::optional<Error>& error,
                     const std::shared_ptr<Entry>& entry) {
  if (!out.sandbox_id.empty() && out.result.info)
    update(out.sandbox_id, *out.result.info);
  if constexpr (std::is_same_v<Response, pb::RunStepResponse>) {
    auto text = result_json(out.result.tool);
    if (text.size() > kRpcLimit - 65536)
      throw Error(Code::PayloadTooLarge, "tool result exceeds RPC limit");
    resp->set_result_json(text);
    resp->set_error_message(error ? error->what() : "");
  } else if constexpr (std::is_same_v<Response, pb::DeleteSandboxResponse>) {
    resp->set_sandbox_id(out.sandbox_id);
    resp->set_cleanup_confirmed(out.cleanup_confirmed);
  } else if constexpr (!std::is_same_v<Response, pb::GetStatusResponse>) {
    if (!out.sandbox_id.empty())
      *resp->mutable_sandbox() = lookup(out.sandbox_id).info;
  }
  resp->set_success(!error);
  auto* detail = resp->mutable_error();
  detail->set_code(error ? error_code(*error, out.result) : pb::ERROR_OK);
  if (error) {
    detail->set_message(error->what());
    detail->set_backend_code(error->backend_code);
  }
  auto* response_meta = resp->mutable_meta();
  response_meta->set_request_id(meta.request_id());
  response_meta->set_trace_id(meta.trace_id());
  const auto total = micros(ctx.start, Clock::now());
  response_meta->mutable_proxy_elapsed()->set_value_ms(total / 1000);
  if (entry) {
    std::lock_guard<std::mutex> lock(mutex_);
    auto* op = entry->record.mutable_info();
    op->set_sandbox_id(out.sandbox_id);
    op->set_observed_at_unix_ms(now_ms());
    op->set_state(ctx.outcome == Outcome::Unknown ? pb::OPERATION_UNKNOWN
                  : error                         ? pb::OPERATION_FAILED
                                                  : pb::OPERATION_SUCCEEDED);
    if constexpr (!std::is_same_v<Response, pb::GetStatusResponse>)
      *resp->mutable_operation() = *op;
    entry->create_absent = op->kind() == pb::OPERATION_CREATE && out.sandbox_id.empty() &&
                           !out.result.info && ctx.outcome != Outcome::Unknown;
    if (op->state() == pb::OPERATION_SUCCEEDED || op->state() == pb::OPERATION_FAILED)
      save_response(*resp, &entry->record);
    const auto bytes = entry->record.ByteSizeLong() + 1024;
    record_bytes_ -= entry->bytes;
    entry->bytes = bytes;
    record_bytes_ += bytes;
  }
  calls << 1;
  if (error)
    failures << 1;
  latency << total;
  LOG(INFO)
      << Json{{"request_id", meta.request_id()},          {"code", detail->code()},
              {"outcome", static_cast<int>(ctx.outcome)}, {"total_us", total},
              {"queue_us", ctx.timing.queue_us},          {"preflight_us", ctx.timing.preflight_us},
              {"operation_us", ctx.timing.operation_us},  {"readiness_us", ctx.timing.readiness_us},
              {"cleanup_us", ctx.timing.cleanup_us},      {"backend_requests", ctx.timing.requests}}
             .dump(-1, ' ', false, Json::error_handler_t::replace);
}

template <class Request, class Response>
void Service::dispatch(google::protobuf::RpcController* controller, const Request* req,
                       Response* resp, google::protobuf::Closure* done, pb::OperationKind kind,
                       bool step, uint32_t timeout_ms, uint32_t max_ms, Operation operation) {
  brpc::ClosureGuard guard(done);
  auto ctx = std::make_shared<OperationContext>();
  ctx->start = Clock::now();
  ctx->deadline = ctx->start + std::chrono::milliseconds(timeout_ms);
  auto* cntl = static_cast<brpc::Controller*>(controller);
  ctx->cancelled = [cntl] { return cntl->IsCanceled(); };
  auto out = std::make_shared<WorkResult>();
  std::shared_ptr<Entry> entry;
  auto complete = [this, ctx, out, req, resp, cntl](const std::optional<Error>& error,
                                                    const std::shared_ptr<Entry>& e) {
    try {
      finish(*ctx, req->meta(), resp, *out, error, e);
    } catch (...) {
      // Preserve uncertainty and prevent a partially constructed response being replayed.
      if (e) {
        std::lock_guard<std::mutex> lock(mutex_);
        e->record.mutable_info()->set_state(pb::OPERATION_UNKNOWN);
        e->record.clear_final_response();
      }
      cntl->SetFailed("response construction failed; query operation status");
    }
    out->lease.reset();
  };
  try {
    validate_meta(req->meta());
    if (req->ByteSizeLong() > kRpcLimit)
      throw Error(Code::PayloadTooLarge, "RPC request too large");
    {
      std::lock_guard<std::mutex> lock(mutex_);
      auto it = records_.find(req->meta().request_id());
      if (it != records_.end()) {
        if (it->second->fingerprint != fingerprint(*req))
          throw Error(Code::RequestIdConflict,
                      "request ID is already bound to a different request");
        throw Error(Code::DuplicateRequest,
                    "request already recorded; query GetStatus with a new ID");
      }
      if (kind != pb::OPERATION_KIND_UNSPECIFIED) {
        if (records_.size() >= max_records_ || record_bytes_ > max_record_bytes_ ||
            kReservation > max_record_bytes_ - record_bytes_)
          throw Error(Code::StorageUnavailable,
                      "operation record capacity exhausted; request not dispatched");
        entry = std::make_shared<Entry>();
        entry->fingerprint = fingerprint(*req);
        entry->bytes = kReservation;
        *entry->record.mutable_original_meta() = req->meta();
        auto* op = entry->record.mutable_info();
        op->set_request_id(req->meta().request_id());
        op->set_kind(kind);
        op->set_state(pb::OPERATION_ACCEPTED);
        op->set_dispatch_state(pb::DISPATCH_NOT_STARTED);
        op->set_observed_at_unix_ms(now_ms());
        if constexpr (std::is_same_v<Request, pb::DeleteSandboxRequest>)
          op->set_target_create_request_id(req->create_request_id());
        records_.emplace(req->meta().request_id(), entry);
        record_bytes_ += entry->bytes;
      }
    }
    if (timeout_ms == 0 || timeout_ms > max_ms)
      throw Error(Code::InvalidArgument, "timeout_ms outside RPC limit");
    ctx->on_dispatch = [this, entry, out](bool acknowledged) {
      if (!entry)
        return;
      std::lock_guard<std::mutex> lock(mutex_);
      auto* info = entry->record.mutable_info();
      info->set_sandbox_id(out->sandbox_id);
      if (acknowledged || info->dispatch_state() != pb::DISPATCH_STARTED)
        info->set_dispatch_state(acknowledged ? pb::DISPATCH_STARTED
                                              : pb::DISPATCH_MAY_HAVE_STARTED);
      info->set_observed_at_unix_ms(now_ms());
    };
    auto work = [this, ctx, out, entry, complete, operation = std::move(operation),
                 done]() mutable {
      brpc::ClosureGuard completion(done);
      std::optional<Error> error;
      ctx->timing.queue_us = micros(ctx->start, Clock::now());
      try {
        ctx->check();
        if (entry) {
          std::lock_guard<std::mutex> lock(mutex_);
          entry->record.mutable_info()->set_state(pb::OPERATION_RUNNING);
          entry->record.mutable_info()->set_observed_at_unix_ms(now_ms());
        }
        operation(*ctx, *out);
      } catch (const Error& e) {
        error = e;
      } catch (const Json::exception&) {
        error = Error(Code::BackendProtocolError, "invalid backend field type");
      } catch (...) {
        error = Error(Code::InternalError, "internal operation failure");
      }
      complete(error, entry);
    };
    if (!(step ? step_pool_ : control_pool_).submit(std::move(work), ctx->deadline))
      throw Error(Code::ResourceExhausted, "proxy work queue is full");
    guard.release();
  } catch (const Error& e) {
    complete(e, entry);
  } catch (...) {
    complete(Error(Code::InternalError, "request dispatch failed"), entry);
  }
}

void Service::CreateSandbox(google::protobuf::RpcController* c, const pb::CreateSandboxRequest* q,
                            pb::CreateSandboxResponse* r, google::protobuf::Closure* d) {
  dispatch(
      c, q, r, d, pb::OPERATION_CREATE, false, q->timeout_ms(), 300000,
      [this, q](auto& ctx, auto& out) {
        validate_identifier(q->backend(), "backend");
        validate_identifier(q->template_(), "template");
        if (!q->has_spec() || q->spec().cpu_count() == 0 || q->spec().memory_mib() == 0 ||
            q->ttl_seconds() == 0 || q->ttl_seconds() > INT32_MAX)
          throw Error(Code::InvalidArgument,
                      "positive spec and ttl_seconds in 1..INT32_MAX required");
        auto& a = adapter(q->backend());
        const auto& templates = configs_.at(q->backend()).templates;
        auto it = templates.find(q->template_());
        if (it == templates.end())
          throw Error(Code::Unsupported, "template has no configured resource specification");
        const auto& t = it->second;
        if (t.cpu_count != q->spec().cpu_count() || t.memory_mib != q->spec().memory_mib())
          throw Error(Code::Unsupported, "requested resources do not match configured template");
        CreateSpec spec;
        spec.backend = q->backend();
        spec.template_id = t.backend_template_id;
        spec.ttl_seconds = q->ttl_seconds();
        spec.wait_ready = true;
        spec.metadata = {{"agent_proxy_instance", instance_id_},
                         {"agent_proxy_request", q->meta().request_id()}};
        try {
          a.create(spec, ctx, out.result);
        } catch (...) {
          if (out.result.info)
            out.sandbox_id = bind(*out.result.info, t, spec.ttl_seconds);
          throw;
        }
        if (!out.result.info || !out.result.info->envd_ready.value_or(false))
          throw Error(Code::BackendProtocolError, "create returned without readiness verification");
        out.sandbox_id = bind(*out.result.info, t, spec.ttl_seconds);
      });
}
void Service::RunStep(google::protobuf::RpcController* c, const pb::RunStepRequest* q,
                      pb::RunStepResponse* r, google::protobuf::Closure* d) {
  r->set_step_id(q->step_id());
  const bool bash = q->tool() == "bash";
  // Bash receives preflight and cleanup allowance in addition to the tool's execution budget.
  const uint32_t budget = q->timeout_ms() > 900000 ? 0 : q->timeout_ms() + (bash ? 7000 : 0);
  dispatch(c, q, r, d, pb::OPERATION_RUN_STEP, true, budget, bash ? 907000 : 120000,
           [this, q, bash](auto& ctx, auto& out) {
             if (!q->step_id() || !q->timeout_ms())
               throw Error(Code::InvalidArgument, "positive step_id and timeout_ms required");
             if (q->tool() != "bash" && q->tool() != "read" && q->tool() != "write" &&
                 q->tool() != "edit")
               throw Error(Code::UnsupportedTool, "unsupported tool");
             auto tool = parse_tool(q->tool(), q->arguments_json(),
                                    bash ? std::optional<uint32_t>(q->timeout_ms()) : std::nullopt);
             auto b = lookup(q->sandbox_id());
             out.sandbox_id = q->sandbox_id();
             acquire(out, out.sandbox_id);
             if (b.info.state() == pb::SANDBOX_DELETED)
               throw Error(Code::InvalidState, "sandbox is deleted");
             adapter(b.ref.backend).run(b.ref, tool, ctx, out.result);
           });
}
void Service::PauseSandbox(google::protobuf::RpcController* c, const pb::PauseSandboxRequest* q,
                           pb::PauseSandboxResponse* r, google::protobuf::Closure* d) {
  dispatch(c, q, r, d, pb::OPERATION_PAUSE, false, q->timeout_ms(), 300000,
           [this, q](auto& ctx, auto& out) {
             auto b = lookup(q->sandbox_id());
             out.sandbox_id = q->sandbox_id();
             acquire(out, out.sandbox_id);
             if (b.info.state() == pb::SANDBOX_DELETED)
               throw Error(Code::InvalidState, "sandbox is deleted");
             adapter(b.ref.backend).pause(b.ref, ctx, out.result);
           });
}
void Service::ResumeSandbox(google::protobuf::RpcController* c, const pb::ResumeSandboxRequest* q,
                            pb::ResumeSandboxResponse* r, google::protobuf::Closure* d) {
  dispatch(c, q, r, d, pb::OPERATION_RESUME, false, q->timeout_ms(), 300000,
           [this, q](auto& ctx, auto& out) {
             auto b = lookup(q->sandbox_id());
             out.sandbox_id = q->sandbox_id();
             acquire(out, out.sandbox_id);
             if (b.info.state() == pb::SANDBOX_DELETED)
               throw Error(Code::InvalidState, "sandbox is deleted");
             adapter(b.ref.backend)
                 .resume({b.ref, b.info.effective_ttl_seconds(), true}, ctx, out.result);
           });
}
void Service::DeleteSandbox(google::protobuf::RpcController* c, const pb::DeleteSandboxRequest* q,
                            pb::DeleteSandboxResponse* r, google::protobuf::Closure* d) {
  dispatch(c, q, r, d, pb::OPERATION_DELETE, false, q->timeout_ms(), 120000,
           [this, q](auto& ctx, auto& out) {
             std::string id;
             if (q->target_case() == pb::DeleteSandboxRequest::kSandboxId)
               id = q->sandbox_id();
             else if (q->target_case() == pb::DeleteSandboxRequest::kCreateRequestId) {
               validate_identifier(q->create_request_id(), "create_request_id");
               std::lock_guard<std::mutex> lock(mutex_);
               auto it = records_.find(q->create_request_id());
               if (it == records_.end())
                 throw Error(Code::NotFound, "create operation is not recorded in this process");
               const auto& info = it->second->record.info();
               if (info.kind() != pb::OPERATION_CREATE)
                 throw Error(Code::InvalidArgument, "target is not a create operation");
               if (info.state() == pb::OPERATION_ACCEPTED || info.state() == pb::OPERATION_RUNNING)
                 throw Error(Code::InvalidState,
                             "create is still active; query its operation status");
               id = info.sandbox_id();
               if (id.empty()) {
                 if (!it->second->create_absent)
                   throw Error(
                       Code::StatusUnavailable,
                       "create outcome unknown; absence of a sandbox ID does not confirm cleanup");
                 out.cleanup_confirmed = true;
                 ctx.outcome = Outcome::Completed;
                 return;
               }
             } else
               throw Error(Code::InvalidArgument, "delete target required");
             auto b = lookup(id);
             out.sandbox_id = id;
             acquire(out, id);
             if (b.info.state() == pb::SANDBOX_DELETED) {
               out.cleanup_confirmed = true;
               ctx.outcome = Outcome::Completed;
               return;
             }
             adapter(b.ref.backend).remove(b.ref, true, ctx, out.result);
             Info deleted;
             deleted.ref = b.ref;
             deleted.state = State::Deleted;
             out.result.info = deleted;
             out.cleanup_confirmed = true;
           });
}
void Service::GetStatus(google::protobuf::RpcController* c, const pb::GetStatusRequest* q,
                        pb::GetStatusResponse* r, google::protobuf::Closure* d) {
  dispatch(c, q, r, d, pb::OPERATION_KIND_UNSPECIFIED, false, 10000, 10000,
           [this, q, r](auto& ctx, auto& out) {
             if (q->target_case() == pb::GetStatusRequest::kOperationRequestId) {
               validate_identifier(q->operation_request_id(), "operation_request_id");
               std::lock_guard<std::mutex> lock(mutex_);
               auto it = records_.find(q->operation_request_id());
               if (it == records_.end())
                 throw Error(Code::NotFound, "operation is not recorded in this process");
               *r->mutable_operation() = it->second->record;
             } else if (q->target_case() == pb::GetStatusRequest::kSandboxId) {
               auto b = lookup(q->sandbox_id());
               if (b.info.state() != pb::SANDBOX_DELETED) {
                 try {
                   adapter(b.ref.backend).status(b.ref, ctx, out.result);
                 } catch (const Error& e) {
                   if (e.code == Code::NotFound) {
                     Info deleted;
                     deleted.ref = b.ref;
                     deleted.state = State::Deleted;
                     out.result.info = deleted;
                   } else
                     throw Error(Code::StatusUnavailable, "cannot observe backend sandbox state");
                 }
                 if (out.result.info)
                   update(q->sandbox_id(), *out.result.info);
                 b = lookup(q->sandbox_id());
               }
               *r->mutable_sandbox() = b.info;
             } else
               throw Error(Code::InvalidArgument, "status target required");
             ctx.outcome = Outcome::Completed;
           });
}
}  // namespace ap
