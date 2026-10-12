# Agent Proxy

C++17 / bRPC 中间层，向 Agent Sandbox Benchmark 暴露六个统一 RPC，通过 libcurl 访问 E2B/KASandbox 控制面 REST API 和 envd Connect API。首版仅包含 E2B 适配器。

## 验证状态

当前开发机为 Windows，无 Linux、Docker 和 C++ 工具链。已运行的检查与尚未运行的检查见 [验证记录](docs/validation.md)。**本地 Mock 测试通过不代表 C++ 编译或完整 bRPC 端到端链路通过。** 服务器应执行下面的构建测试命令再验收。

## Linux 服务器构建与测试

建议首先使用 Ubuntu 22.04、GCC 11、系统 Protobuf 3.12、bRPC 1.12.1。其他发行版可安装等价开发包。构建脚本不会执行 sudo 或修改系统软件。

首次准备依赖：

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake git pkg-config ca-certificates python3 \
  libssl-dev libgflags-dev libprotobuf-dev libprotoc-dev protobuf-compiler \
  libleveldb-dev zlib1g-dev libcurl4-openssl-dev nlohmann-json3-dev
```

完整的克隆、安装依赖、测试及日志回传命令见 [Linux 验证清单](docs/linux-validation.md)。克隆仓库后进入 `agent_proxy` 目录执行：

```bash
bash scripts/build_and_test.sh
```

该命令将：

1. 首次运行下载 bRPC 1.12.1，在项目 `.deps` 中编译和安装。
2. 用 CMake 构建 `agent-proxy`、`agent-proxy-client`、`core-tests`。
3. 执行 C++ 核心测试、Python Mock 协议测试和完整 bRPC 端到端测试。

可用 `JOBS=4` 调整构建并行度，用 `BRPC_ROOT=/path/to/brpc` 复用已安装的兼容 bRPC。离线服务器需要预先安装依赖并提供 `BRPC_ROOT`。所有组件应使用同一套 Protobuf 库，避免 ABI 混用。

端到端测试会分配本机临时端口、启动和清理子进程；失败时返回非零退出码。报告在 `build/test-results/e2e.json`，同目录保留每次运行的 Mock/Proxy 日志、请求和响应。这些请求只含 Mock 数据。

如果服务器有 Docker，也可以直接执行：

```bash
docker build -t agent-proxy-test .
docker run --rm agent-proxy-test
```

Docker 构建阶段同样执行全部测试；本开发机尚未验证镜像构建。

## 手动运行 Mock 链路

在三个 Linux 终端中分别运行：

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

复制创建响应的 `sandbox.ref` 到后续请求，例如：

```json
{
  "context": {"request_id": "step-1", "timeout_ms": 65000},
  "ref": {"backend": "mock", "sandbox_id": "mock-1"},
  "tool": "bash",
  "arguments_json": "{\"command\":\"printf hello\"}"
}
```

保存为请求文件后，调用客户端 `--method=RunStep --request=/path/to/request.json`。客户端使用原生 `baidu_std` bRPC 协议，不通过 HTTP JSON 绕过 RPC 层；`--request=-` 可读取 stdin。

客户端退出码：0 为 `status=OK`，2 为结构化业务/后端错误，3 为 RPC 传输失败，1 为本地配置或输入错误。bash 非零退出仍返回客户端退出码 0，任务层应检查 `bash.exit_code`。

## 连接真实后端

从 `configs/e2b.example.json` 复制所需后端项，删除不用的项，设置各项 `api_key_env` 指定的环境变量。配置中不保存 API Key。

- `api_url`：控制面 API 根地址。
- `envd_url_pattern`：可选固定网关或包含 `{sandbox_id}` 的 URL。
- 未设置 `envd_url_pattern` 时，使用 `envd_scheme://49983-{id}.{domain}`，domain 优先采用后端响应，再采用 `sandbox_domain`。
- 数据面携带 `X-Access-Token`、`E2b-Sandbox-Id`、`E2b-Sandbox-Port`。可选 `user` 控制命令和文件操作身份。
- `profile` 接受 `kasandbox`、`e2b_cloud`；目前二者使用同一份已核对的 REST/Connect 契约。profile 明确标识测试目标，不意味着云端差异已完成实机验证。

真实后端测试必须由部署方提供 API 地址、模板、凭据和可达的数据面域名。Mock 的 `base` 模板与 `mock:*` 命令不能用于真实后端。

## 实现范围

详见 [接口与语义](docs/interface.md)。主要约定：

- 六个同步 RPC，RunStep 支持 bash/read/write/edit。
- 默认要求 Create/Resume 的 envd `/health` 可用；可以 `wait_ready=false` 只等待控制面确认。
- 同一实例内，同一沙箱的 RunStep/Pause/Resume/Delete 互斥；GetStatus 可并发。
- 独立控制面和工具工作池，有界队列；超时包含排队和前置查询。
- 无自动业务重试、无幂等缓存，`request_id` 只用于追踪。
- libcurl 为有副作用的目标请求使用新连接，防止失效复用连接触发透明重放；只读请求复用连接。Benchmark 应记录这一连接策略。
- 文件工具支持 UTF-8 文本；单文件上限 8 MiB。输出分别保留 stdout/stderr 前 1 MiB。
- 关闭连接不等于杀死命令；错误/超时时尝试向已知 PID 发送 SIGKILL，不把信号确认当成退出确认。

Mock 是内存协议模型，**不会执行主机 shell，也不模拟真实虚拟机或快照性能**。支持 `printf hello`、`exit 2`、`mock:stderr`、`mock:echo:文本`、`mock:sleep:毫秒` 等确定性命令；未知命令返回 127。故障注入仅存在于 Mock。

运行参数：`--control_threads=4`、`--step_threads=16`、`--queue_size=128`。默认仅监听回环地址。`--admin_port` 可启用 bRPC 内部诊断端口，提供 `agent_proxy_calls`、`agent_proxy_failures`、`agent_proxy_latency` 等 bvar 指标；默认关闭内部服务。首版没有对外部多租户调用方实现身份认证，部署范围是受控 Benchmark 网络。

## 项目结构

```text
proto/agent_proxy.proto       六个 RPC、请求响应和错误枚举
src/core/                    内部类型、deadline、有界线程池、沙箱互斥
src/service/                 bRPC 服务、调度和响应转换
src/adapters/e2b/            E2B/Envd 适配和 Connect 编解码
src/transport/               libcurl 传输、错误映射、阶段计时
src/tools/                   工具参数校验、文本替换
src/config/                  后端配置读取
client/                      通用原生 bRPC 测试客户端
tests/unit/                  C++ 核心测试
tests/mock_backend/          Python 协议 Mock 及其测试
tests/integration/           完整 bRPC 链路测试驱动
scripts/build_and_test.sh    Linux 构建及验收入口
```

尚不包含 AgentENV/CubeSandbox、分布式锁、持久化操作账本、后台命令句柄或客户端流式输出。进程重启不保存本地操作状态；对已有沙箱可用 `(backend, sandbox_id)` 重新查询。
