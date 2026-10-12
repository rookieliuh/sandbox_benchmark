# Agent Proxy — benchmark.agent.v1

C++17 / bRPC 服务，提供 CreateSandbox、RunStep、PauseSandbox、ResumeSandbox、DeleteSandbox、GetStatus，通过 E2B 控制面 REST 和 envd Connect API 执行操作。AgentENV 和 CubeSandbox 尚未实现。

## 协议

`proto/agent_proxy.proto` 使用协作方提供的 proto3 契约，包名 `benchmark.agent.v1`，服务名 `AgentProxyService`。本次迁移不修改协作方字段、编号或枚举。

这是一次不兼容升级。Client 和 Proxy 必须共同使用此版本重新生成代码；不能混用旧的 `agentproxy.v1` 请求。

- [接口语义与结果 JSON](docs/interface.md)：发送给 Client 开发同事一起对齐。
- [Linux 构建与日志](docs/linux-validation.md)。
- [真实 E2B 验证步骤](docs/e2b-validation.md)。
- [验证记录](docs/validation.md)：区分旧版本已通过的服务器测试和本次迁移待验证项。

## 在已有 Linux 服务器上更新

```bash
(
  set -euo pipefail
  cd /home/agent_proxy/sandbox_benchmark/agent_proxy
  git pull --ff-only origin main
  export JOBS=1 CTEST_PARALLEL_LEVEL=1
  export BRPC_ROOT="$PWD/.deps/brpc-install" BUILD_DIR="$PWD/build"
  export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}"
  export no_proxy="127.0.0.1,localhost${no_proxy:+,$no_proxy}"
  nice -n 10 bash scripts/validate_linux.sh
)
```

构建脚本自动加载项目 `.deps/local` 中已经安装的依赖。不会运行系统包管理器、修改系统动态库配置或重启已有服务。编译仍消耗资源，安装路径也不构成 root 权限隔离。

首次构建需要 Linux、GCC/C++17、CMake >=3.20、Python >=3.10、Protobuf（含 protoc 和 libprotoc 开发文件）、libcurl、OpenSSL、zlib、gflags、LevelDB、nlohmann/json >=3.9；脚本会按需在 `.deps` 内编译安装 bRPC 1.12.1。现有服务器使用 Protobuf 3.14.0、LevelDB 1.23、nlohmann/json 3.11.3。

CTest 保留三组：`core`、`mock-protocol`、`e2e`。e2e 启动真实 C++ Client/Proxy 和 Python Mock；还用 Mock 验证真实 E2B 冒烟脚本本身，不会连接真实 E2B。报告为 `build/test-results/e2e.json`。

## 手动运行

分别在终端启动 Mock、Proxy、Client。每个运行 C++ 二进制的终端先在项目目录执行 `source scripts/use_local_deps.sh`，或将下面命令放进子 Shell。

```bash
python3 tests/mock_backend/server.py --port 18080
```

```bash
export MOCK_E2B_API_KEY=mock-key
./build/agent-proxy --listen=127.0.0.1:8080 --backends=configs/mock.json
```

```bash
./build/agent-proxy-client --server=127.0.0.1:8080 \
  --method=CreateSandbox --request=configs/create.example.json
```

从响应取 `sandbox.sandbox_id`（Proxy ID，不是后端 ID），用于后续请求。例如：

```json
{
  "sandbox_id": "替换为创建响应中的ID",
  "step_id": 1,
  "tool": "bash",
  "arguments_json": "{\"command\":\"printf hello\"}",
  "timeout_ms": 10000,
  "meta": {"request_id": "step-example-1", "task_instance_id": "example-replay"}
}
```

客户端使用 `baidu_std`，自动重试为 0；`--request=-` 接收 stdin。退出码：0 表示 `success=true && error.code=ERROR_OK`；2 为结构化错误；3 为传输失败且结果可能未知；1 为本地输入/配置错误。命令非零退出仍是有效工具结果，检查 `result_json` 中的 `exit_code`。

## 状态与边界

- Create/Resume 成功必须健康检查通过；Pause 轮询控制面确认 paused；Delete 后确认 404，才报告清理确认。
- 资源规格通过配置的 `templates` 映射精确校验。模板实际 CPU/内存需部署方核实，不会通过忽略参数伪造符合规格的成功。
- 同一实例内每个沙箱的 RunStep/Pause/Resume/Delete 互斥，GetStatus 可并发；控制与工具使用独立有界工作池。
- 相同请求 ID 不重新执行；相同内容返回 `ERROR_DUPLICATE_REQUEST`，不同内容返回 `ERROR_REQUEST_ID_CONFLICT`。原始结果用新 ID 调用 GetStatus 查询。
- 操作记录与 Proxy 沙箱绑定仅保存在进程内，默认最多 10000 条操作、256 MiB 记录预算（包括每个在途操作约 16 MiB 的预留）。不自动淘汰记录；容量不足在执行前返回 `ERROR_STORAGE_UNAVAILABLE`。参数为 `--max_records`、`--record_memory_mib`。
- 没有持久化/跨实例恢复。运行一次 benchmark 时固定使用同一 Proxy 进程，清理结束前不要重启。重启后未知 ID 返回 NOT_FOUND，并不证明后端资源不存在。
- 创建结果未知且无 ID 时，按创建请求 ID 删除返回 `ERROR_STATUS_UNAVAILABLE` 和 `cleanup_confirmed=false`。需要部署方按创建 metadata 查找/清理，不保证自动发现迟到创建的资源。
- 默认只监听回环地址；没有多租户认证。真实配置放在忽略的 `configs/*.local.json`，密钥使用环境变量；不要提交密钥。

HTTP 目标变更请求禁用连接复用以避免 libcurl 的透明重放；没有自动业务重试。Mock 只模拟协议，不能证明真实 E2B 可用性、性能或全内存快照语义。
