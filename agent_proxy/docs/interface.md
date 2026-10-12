# Agent Proxy v1 接口约定

IDL 为 `proto/agent_proxy.proto`，包名 `agentproxy.v1`，服务名 `AgentProxyService`。所有消息使用 proto2 optional 字段保证默认值和存在性；业务必填字段由服务端校验，以返回统一错误。

## 公共消息

请求 `context.request_id` 必填（1～256 字节、无 NUL/换行），只作追踪，不作去重。`context.timeout_ms` 省略使用方法默认值，0 非法。Ref 的 `backend` 是服务端配置名，`sandbox_id` 仅接受字母、数字、下划线、连字符。

所有响应含 `context.status`、`context.outcome`、`context.timing` 和实际后端请求数。失败时可能保留部分业务结果，例如创建成功后健康检查超时仍保留 sandbox.ref，命令超时仍保留部分输出。

outcome：

- NOT_STARTED：目标操作尚未提交，如参数校验或前置查询失败。
- COMPLETED：已知结果（包括拒绝、命令非零退出、创建成功但后续健康检查失败）。
- UNKNOWN：目标请求可能执行，但未获得可靠结果；必须先协调状态或读回数据。

`retry_advice` 仅是建议，Proxy 不自动重试；UNKNOWN 返回 RECONCILE_FIRST。RPC 传输失败时可能没有响应，需要客户端按不确定结果处理。客户端超时不保证服务端感知取消，显式 Proxy 预算仍会生效。

SandboxInfo.state 表示控制面报告的 RUNNING/PAUSED。envd_ready 只有执行过健康检查才存在。GetStatus 不返回缓存状态，404 为 NOT_FOUND，不推断已删除。DELETED 枚举为后续扩展保留，Delete 当前只返回引用及确认依据。

## RPC

| 方法 | 请求业务字段 | 响应业务字段 |
|---|---|---|
| CreateSandbox | backend、template_id 必填；ttl_seconds=300、env_vars、metadata、wait_ready=true | sandbox、completion_basis |
| RunStep | ref、tool、arguments_json；bash 可选 execution_timeout_ms=60000 | bash/read/write/edit oneof |
| PauseSandbox | ref | sandbox、already_paused、completion_basis |
| ResumeSandbox | ref、ttl_seconds=300、wait_ready=true | sandbox、already_running、completion_basis |
| DeleteSandbox | ref、ignore_missing=true | ref、already_absent、completion_basis |
| GetStatus | ref | sandbox |

Create 显式设置 secure=true、autoPause=false、autoResume.policy=off。TTL 必须在 1..INT32_MAX 秒，最终还受后端限制。Resume 使用 `/connect`，既恢复暂停沙箱，也可延长运行中沙箱的 TTL；不能缩短已有 TTL。Create/Resume 默认经 `/health` 就绪检查返回 ENVD_READY；关闭 wait_ready 返回 BACKEND_ACK。

Pause 要求后端全内存暂停，不做文件系统快照降级。HTTP 409 时查询沙箱，只有确认 PAUSED 才转换为成功。参考库可能将 pausing 报告为 paused，因此成功不意味着 Proxy 额外验证了持久化快照。Delete 成功为后端确认，不保证宿主资源即时回收；仅在 ignore_missing=true 时将 404 视为成功。

## 工具

arguments_json 必须是 object，未知字段报错。字段名区分大小写；文件路径和显式 cwd 必须是沙箱内绝对路径。

| 工具 | 必填参数 | 可选参数 | 输出 |
|---|---|---|---|
| bash | command | cwd、env | stdout/stderr bytes、可选 exit_code、termination、PID、截断和退出确认标记、实际执行预算 |
| read | path | 无 | UTF-8 content、size_bytes |
| write | path、content | 无 | bytes_written |
| edit | path、oldString、newString | 无 | replacement_count、bytes_written |

bash 运行 `/bin/bash -l -c`，stdin=false。env 键采用 shell 环境变量标识符格式；command 和 env 值禁止 NUL。HTTP/Connect 的非零退出可能附带 `error: "exit status N"`，这是命令结果，返回 status=OK。end.exited=false 的完整终止事件标记 SIGNALLED。无有效退出事件则不填 exit_code。

write 覆盖文件，先通过 Filesystem.Stat 检查父目录，因为 envd 上传接口会自动创建父目录。该检查与上传之间仍可能被外部修改，首版不保证全局原子性。edit 读回全文后做唯一字面匹配（含重叠匹配），再写回；零匹配、多匹配、空 oldString 均不写。所有文件内容是 UTF-8，文本可含 NUL；二进制 read 返回 UNSUPPORTED。

同一 Proxy 实例的 RunStep/Pause/Resume/Delete 互斥；冲突返回 SANDBOX_BUSY。外部 SDK、沙箱内部进程和其他 Proxy 实例不受该锁保护。每次 RunStep 都前置 GET 详情，确认运行并取得数据面 token/版本；该检查不消除之后的外部竞态，也不触发自动恢复/续期。

单文件 8 MiB、单 RPC 16 MiB、command 1 MiB、stdout/stderr 各保留前 1 MiB。输出超出限制仍消费后续事件，直到退出；文件超限返回错误，不当作完整 read 成功。

## 超时与清理

| RPC | 默认预算 | 上限 |
|---|---:|---:|
| Create/Pause/Resume | 60s | 300s |
| RunStep bash | 65s | 900s |
| RunStep 文件 | 30s | 120s |
| Delete | 30s | 120s |
| GetStatus | 10s | 30s |

使用单调时钟计算统一 deadline。排队、前置检查、实际调用、健康检查均共享预算。建连最长 3 秒，仍受剩余预算限制。bash 实际预算为 min(execution_timeout_ms, 剩余预算-2秒)，通过 Connect-Timeout-Ms 传递，并由 libcurl 本地计时约束。

所有异常中断的命令流，若已有 PID 且未见终止事件，会尝试 SendSignal(SIGKILL)，单次清理最多 2 秒且不越过总 deadline。信号被接受也不将 termination_confirmed 设为 true；只有收到终止事件才确认退出。主机不可达、未拿到 PID、进程逃逸等情况下无法保证清理，Benchmark 应删除沙箱收尾。没有接入后台进程或 PTY 管理。

测试客户端默认传输超时为 Proxy 预算+2秒，max_retry=0；服务端不读取客户端私有超时字段来替代明确的操作预算。

## 错误码

| 编号 | 名称 |
|---|---|
| 0 | OK |
| 1001/1002/1003 | INVALID_ARGUMENT / UNSUPPORTED / PAYLOAD_TOO_LARGE |
| 1101/1102/1103/1104 | NOT_FOUND / INVALID_STATE / EDIT_CONFLICT / SANDBOX_BUSY |
| 1201/1202 | UNAUTHENTICATED / PERMISSION_DENIED |
| 1301/1302 | RATE_LIMITED / RESOURCE_EXHAUSTED |
| 1401/1402 | DEADLINE_EXCEEDED / CANCELLED |
| 1501/1502/1503 | BACKEND_UNAVAILABLE / BACKEND_ERROR / BACKEND_PROTOCOL_ERROR |
| 1901 | INTERNAL_ERROR |

status 可包含 source、resource_type、HTTP 状态、Connect 错误码、Retry-After 秒数转换的 retry_after_ms。原始后端消息不透传，以免暴露凭据或命令数据。HTTP 200 的 Connect 流仍需检查 EndStreamResponse。缺帧、坏 JSON、缺少退出事件、结束后还有数据等均返回 BACKEND_PROTOCOL_ERROR。

## E2B 兼容边界与计时

控制面使用 `/sandboxes`、`/{id}`、`/{id}/pause`、`/{id}/connect`；envd 使用 `/process.Process/Start`、`/process.Process/SendSignal`、`/filesystem.Filesystem/Stat`、`/files`、`/health`。Start 使用 Connect JSON framing，普通 unary RPC 使用 application/json。未实现通用 gRPC 客户端。

要求可识别的 envd 稳定版本 >=0.3.0。版本 <0.4.0 且未配置 user 时采用 user 身份；>=0.5.7 使用 octet-stream 上传，否则 multipart。未知或预发布版本明确拒绝，不猜测能力。云端 profile 仍需真实环境契约测试。

timing 使用微秒：proxy_total_us 包含整个服务处理；queue_us 当前包含入队前校验及工作队列等待；preflight/operation/cleanup 统计相应 HTTP 请求时长；readiness 额外包含轮询间隔；backend_total_us 为全部实际 HTTP 请求时长之和。字段不必严格相加等于总时长（另有解析及本地处理）。后端请求数量包括健康检查和清理。客户端独立记录 rpc_wall_ms，不能将 Proxy 观测的 HTTP 耗时视为沙箱内部纯执行时间。
