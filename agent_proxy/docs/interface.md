# benchmark.agent.v1 接口语义

IDL：`proto/agent_proxy.proto`，与协作方提供的文件一致。服务使用原生 `baidu_std` bRPC、proto3、`cc_generic_services=true`。本文补充 IDL 没有表达的 JSON 结构、预算和运行边界，不添加协议字段。

## 请求与响应

所有请求的 `meta.request_id` 必填、1～256 字节、无 NUL/换行；meta 总长不超过 8 KiB，phase 必须是声明过的枚举。其他 benchmark 标签可省略，原样保存在操作记录中。`meta.trace_id` 回显到响应。`ResponseMeta.request_id` 始终属于本次 RPC，不会被原操作 ID 替换。

除 GetStatus 外，timeout_ms 必须显式为正；Create 还要求正 CPU、memory_mib、TTL。RunStep.step_id 必须为正；从 1 开始的编号及 task_instance_id 内的顺序由 Client 维护，Proxy 不按 step_id 去重。

响应必须同时满足 success=true、error.code=ERROR_OK 才表示成功。ERROR_OK 数值为 1，0 表示未指定，不可当成功。RunStep.error_message 始终与 error.message 相同。

## 六个 RPC

| 方法 | 语义 |
|---|---|
| CreateSandbox | backend+template 查配置；spec 必须精确匹配模板；创建并验证 envd 健康后成功。健康检查失败仍保留已获取资源的 Proxy ID。 |
| RunStep | 用 Proxy sandbox_id 找到不可变的后端绑定；解析 arguments_json 对象后运行 bash/read/write/edit；结果为 result_json。 |
| PauseSandbox | 请求暂停并轮询控制面直到 paused；204 本身不代表暂停完成。 |
| ResumeSandbox | 恢复/连接并检查 envd；使用创建时接受的 TTL，Proxy ID 不变；不能借此缩短后端已有 TTL。 |
| DeleteSandbox | oneof 为 sandbox_id 或 create_request_id。对已知资源删除并观察控制面 404；返回 cleanup_confirmed。 |
| GetStatus | oneof 为 sandbox_id 或 operation_request_id。查询沙箱会访问后端；操作查询读取本进程记录及原始最终响应。 |

`SandboxInfo.sandbox_id` 是 `ap-<随机进程标识>-<序号>`，不接受裸后端 ID。绑定不受调用者改变；成功删除留下墓碑，重复删除成功。对已知 ID 观察到后端 404 后标记 DELETED；对从未记录的 ID 返回 NOT_FOUND，不推断资源不存在。

READY 要求本次就绪检查通过；PAUSED 来自后端状态；过渡中或未完成健康检查时返回 UNKNOWN。GetStatus 的读取可以成功且目标状态 UNKNOWN。读取后端失败时返回 ERROR_STATUS_UNAVAILABLE。observed_at_unix_ms 是观察时间，不是耗时。

## 模板与资源

后端配置 templates 中每个别名需指定 backend_template_id、cpu_count、memory_mib。Create.template 选择别名，SandboxInfo.template 返回实际后端模板 ID（优先用后端响应）。effective_spec 来自经部署方核实的模板配置；effective_ttl_seconds 为后端接受的创建 TTL，不是随时间递减的剩余寿命。

未知后端返回 ERROR_UNSUPPORTED_BACKEND；未配置模板规格或请求规格不匹配返回 ERROR_UNSUPPORTED_CAPABILITY；CPU/内存为 0 返回 ERROR_INVALID_ARGUMENT。不会擅自修改模板资源、忽略 spec，或把 MiB 当十进制 MB。若部署改变模板规格，需要同时更新配置并重新验收。

## 工具参数与 result_json

arguments_json 必须是对象，拒绝未知字段；文件路径及显式 cwd 必须为沙箱内绝对路径。

| tool | 必填参数 | 可选参数 |
|---|---|---|
| bash | command | cwd、env |
| read | path | 无 |
| write | path、content | 无 |
| edit | path、oldString、newString | 无 |

bash 运行 `/bin/bash -l -c`，非交互模式。典型 result_json 解析后为：

```json
{
  "tool": "bash", "stdout": "hello", "stderr": "",
  "stdout_base64": "aGVsbG8=", "stderr_base64": "",
  "exit_code": 0, "pid": 123,
  "termination": "EXITED", "termination_confirmed": true,
  "stdout_truncated": false, "stderr_truncated": false,
  "effective_execution_timeout_ms": 10000
}
```

exit_code、pid 未知时为 JSON null，不能把未知退出码写成 0。stdout/stderr 是 UTF-8 显示文本，无效字节替换；base64 字段保留截断后的精确字节。termination 为 UNKNOWN/EXITED/TIMED_OUT/CANCELLED/SIGNALLED。非零退出是有效工具返回，success=true；任务是否通过由 Client 判定。

其他工具结果：

```json
{"tool":"read","content":"文本","size_bytes":6}
{"tool":"write","bytes_written":6}
{"tool":"edit","replacement_count":1,"bytes_written":6}
```

失败前没有工具结果时 result_json 为字符串 `"null"`；有部分 bash 结果时仍保留输出和未知退出码。JSON 内字节计数是整数；外层 Protobuf JSON 的 uint64 字段按 protobuf 规则通常为字符串。

文本文件 UTF-8，允许内容 NUL；单文件上限 8 MiB，stdout/stderr 各保留前 1 MiB。最终序列化结果仍受 16 MiB RPC 上限约束。write 覆盖但要求父目录存在；edit 要求非空 oldString 唯一字面匹配（含重叠匹配），读改写不对其他进程保证原子性。

## 超时

| 方法 | timeout_ms 含义 | 上限 |
|---|---|---:|
| Create/Pause/Resume | 排队、请求、状态/健康检查的总预算 | 300000 |
| Delete | 排队、删除、缺失确认的总预算 | 120000 |
| RunStep bash | 命令执行预算；Proxy 另加 7000ms 用于排队/前置和清理 | 900000 |
| RunStep 文件 | 本次工具操作总预算，含排队/前置 | 120000 |
| GetStatus | 无请求超时字段；Proxy 固定 10000ms | 10000 |

bash 实际预算为 min(请求预算, Proxy 剩余预算-2000ms)。不保证排队后仍能获得完整执行时长。客户端传输超时默认 Proxy 总预算+2000ms，max_retry=0；调用者超时或断连不保证远端立即停止。

工具执行超时返回 ERROR_TOOL_TIMEOUT；排队、前置或生命周期超时返回 ERROR_OPERATION_TIMEOUT。对异常中断的命令流，已知 PID 时尝试 SIGKILL；发送信号的确认不等于退出确认。必须通过沙箱清理收尾。

## 操作记录与重复请求

Mutation 入队前原子登记 request_id 和请求摘要；摘要包含消息类型、所有字段及 meta。相同 ID/内容返回 ERROR_DUPLICATE_REQUEST，内容不同返回 ERROR_REQUEST_ID_CONFLICT，均不重做后端调用，也不覆盖原记录。以新的 meta.request_id 调用 GetStatus(operation_request_id=原ID) 读取结果。

- ACCEPTED：登记且等待执行。
- RUNNING：正在处理，可能尚未向后端提交。
- SUCCEEDED/FAILED：已知最终结果，OperationRecord 恰好含一个匹配 kind 的原始 final_response。
- UNKNOWN：提交结果不确定，或无法构造/保存完整响应；不返回 final_response。
- dispatch_state：尚未向目标后端发送为 NOT_STARTED；请求可能送达为 MAY_HAVE_STARTED；收到 HTTP 响应或进程启动事件为 STARTED。STARTED 不等于成功。

只读 GetStatus 不生成操作记录，不缓存读取请求 ID；Client 仍必须给每次读取分配新 ID。它不得复用 mutation 的 ID。

记录仅存在当前进程：不持久化、不跨实例、不自动淘汰、不保证 exactly-once。默认最多 10000 条，记录预算 256 MiB，每个在途操作预留约 16 MiB；容量不足在执行前返回 ERROR_STORAGE_UNAVAILABLE。查询已有操作不消耗 mutation 配额。请求摘要不保存原始命令，但原始响应可能含命令输出和文件内容。

创建未提交或已明确拒绝且无资源时，Delete(create_request_id) 可以确认无需清理；创建仍执行时返回 ERROR_INVALID_STATE；创建结果未知且没有 ID 时返回 ERROR_STATUS_UNAVAILABLE、cleanup_confirmed=false，不把“没拿到 ID”解释为“没创建”。没有实现自动列举并收敛未知创建；创建 metadata 含 agent_proxy_instance、agent_proxy_request，可供部署方人工核对。

控制面 404 是本实现对已知资源的清理确认依据，不证明宿主物理资源已经回收。后端必须保证删除的资源不会再次出现。若部署将 pausing 提前报告为 paused，Proxy 无法额外证明完整内存快照已持久化；需真实部署验收。

## 错误与计时

严格使用协作方 ErrorCode 数值。鉴权、限流、后端协议/传输异常映射 ERROR_BACKEND_ERROR；文件编辑冲突、沙箱忙及队列满映射 ERROR_INVALID_STATE；不支持的工具映射 ERROR_UNSUPPORTED_TOOL，其他能力缺失映射 ERROR_UNSUPPORTED_CAPABILITY；超大输入/输出映射 ERROR_INVALID_ARGUMENT（响应构造超限可能为传输失败）。不自行扩充 IDL 枚举。

proxy_elapsed 是 Proxy 总毫秒数，存在且为 0 表示实际测量不足 1ms；不是未测量。旧版阶段微秒计时保留在 Proxy JSON 日志中，避免修改协作方 IDL。客户端需独立测量 RPC 墙钟耗时，不能把 Proxy 耗时当成沙箱内纯执行耗时。
