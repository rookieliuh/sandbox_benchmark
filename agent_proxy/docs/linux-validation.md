# Linux 服务器验证命令清单

以下命令适用于 Ubuntu 22.04 / Debian 系 apt 环境，使用 Bash。推荐先在 Ubuntu 22.04 验证（GCC 11、Protobuf 3.12、bRPC 1.12.1）。首轮运行只使用本机 Mock，不需要真实沙箱 API Key，也不需要 root 运行服务。

仓库根目录只包含 `agent_proxy/`。二进制名称继续使用 `agent-proxy` 和 `agent-proxy-client`。

## 1. 安装 Git 并克隆

```bash
sudo apt-get update
sudo apt-get install -y git ca-certificates

mkdir -p "$HOME/workspace"
cd "$HOME/workspace"
git clone https://gitcode.com/fringefatfish/sandbox_benchmark.git
cd sandbox_benchmark/agent_proxy
git rev-parse HEAD
```

如果仓库是私有仓库，使用 GitCode 配置的认证方式。不要将访问令牌直接写进 URL 或日志。

## 2. 安装构建依赖并保存日志

```bash
mkdir -p validation-logs
set -o pipefail

sudo apt-get update 2>&1 | tee validation-logs/apt-update.log
sudo apt-get install -y \
  build-essential cmake git pkg-config ca-certificates python3 \
  libssl-dev libgflags-dev libprotobuf-dev libprotoc-dev protobuf-compiler \
  libleveldb-dev zlib1g-dev libcurl4-openssl-dev nlohmann-json3-dev \
  2>&1 | tee validation-logs/apt-install.log
```

上述安装命令应返回 0；若安装失败，先保留 apt 日志再处理。安装依赖之外的步骤不需要 sudo。

## 3. 构建并运行全部测试

仍在 `sandbox_benchmark/agent_proxy` 目录执行：

```bash
# 关闭继承的自定义构建目录，保证下面日志路径与默认流程一致。
unset BUILD_DIR
export JOBS=2
export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}"
export no_proxy="$NO_PROXY"

bash scripts/validate_linux.sh
validation_status=$?
printf 'Validation exit code: %s\n' "$validation_status"
```

首次运行会从 GitHub 获取 bRPC 1.12.1，编译到项目 `.deps/`，因此需要能访问 GitHub，且首次构建可能较慢。`JOBS=2` 用于限制并行内存开销；资源充足可设为 4。如果已经安装兼容 bRPC，可先设置 `export BRPC_ROOT=/absolute/path/to/brpc`。离线环境需提前准备系统依赖和 bRPC。

`validate_linux.sh` 依次执行构建和 CTest，并在成功或失败后收集日志，不会用 tee 的成功状态覆盖编译失败。它会运行：

- `core`：C++ 核心测试。
- `mock-protocol`：Python Mock 协议测试。
- `e2e`：真实 C++ bRPC 客户端 → C++ Proxy → HTTP/Connect Mock。

测试驱动自动选择回环临时端口并清理自己启动的进程。正常结果为 CTest 输出 `100% tests passed` 且 Validation exit code 为 0。不需要手动额外启动 Mock 或 Proxy。

## 4. 查看端到端报告

```bash
cat validation-logs/last-exit-code.txt
cat validation-logs/last-build-log.txt

if [ -f build/test-results/e2e.json ]; then
  python3 -m json.tool build/test-results/e2e.json
fi
```

完整通过还要求本次 `e2e.json` 中 `passed` 为 true。如果本次编译已失败，旧的 e2e 报告不能视为本次通过。

## 5. 失败时回传日志

脚本会打印一个压缩包的绝对路径，例如：

```text
.../sandbox_benchmark/agent_proxy/test-results/agent-proxy-diagnostics-20261012T080000Z-12345.tar.gz
```

请回传**本次生成的诊断压缩包**，并说明执行的命令及终端打印的 Validation exit code。包内包含：

| 内容 | 用途 |
|---|---|
| validation-logs/environment.txt | 系统版本、架构、工具版本、依赖版本、Git 提交号 |
| validation-logs/apt-*.log | 系统依赖安装失败详情 |
| validation-logs/build-and-test-*.log | bRPC/项目配置、编译、链接及 CTest 完整输出 |
| validation-logs/last-exit-code.txt | 最近一次构建测试退出码 |
| build/Testing/Temporary/ | CTest 详细日志与失败测试列表 |
| build/test-results/e2e.json | 端到端结果与失败堆栈 |
| build/test-results/e2e-*/ | Mock/Proxy 日志、每次客户端 stderr、请求和响应 |
| 两级 CMakeCache 和 CMake 配置日志（存在时） | 项目与 bRPC 的依赖查找、配置错误 |

编译失败时没有 e2e 报告是正常的。收集脚本只打包已存在的诊断文件，不会打包整个 `.deps`、源码参考库、环境变量值或真实后端配置。这里的 e2e 请求响应只使用 Mock 数据。

若在安装阶段失败，或需要手动重新打包，在 `agent_proxy` 目录执行：

```bash
bash scripts/collect_diagnostics.sh
```

## 6. 修复后更新并重跑

```bash
cd "$HOME/workspace/sandbox_benchmark"
git pull --ff-only
cd agent_proxy
bash scripts/validate_linux.sh
```

仅重跑端到端测试（前提是已成功构建当前代码）：

```bash
set -o pipefail
ctest --test-dir build -R '^e2e$' --output-on-failure -V \
  2>&1 | tee validation-logs/e2e-rerun.log
e2e_status=${PIPESTATUS[0]}
printf 'E2E exit code: %s\n' "$e2e_status"
bash scripts/collect_diagnostics.sh
```

首版在开发机只完成 Mock 与静态检查，C++ 编译、链接及完整 bRPC 链路须以服务器实际结果为准。
