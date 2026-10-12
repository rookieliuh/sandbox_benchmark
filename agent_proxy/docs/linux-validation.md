# Linux 验证清单（共享服务器）

目标环境：用户现有 openEuler 24.03 / ARM64 服务器。仓库根目录只包含 agent_proxy，交接文档及参考库不提交。

## 克隆或更新

已有仓库只需更新，不要重新安装系统软件：

```bash
cd /home/agent_proxy/sandbox_benchmark/agent_proxy
git pull --ff-only origin main
git log -1 --oneline
```

新目录才需要克隆（普通开发账号的自有目录）：

```bash
mkdir -p "$HOME/workspace"
cd "$HOME/workspace"
git clone https://gitcode.com/fringefatfish/sandbox_benchmark.git
cd sandbox_benchmark/agent_proxy
```

不要在 Git URL 中放访问令牌。

## 依赖

已有服务器的项目专用依赖目录：

- `.deps/local`：Protobuf 3.14.0 的头文件/库/protoc、LevelDB 1.23、nlohmann/json 3.11.3。
- `.deps/brpc-install`：bRPC 1.12.1。
- 系统已提供 GCC、CMake、gflags、OpenSSL、libcurl、zlib 的开发依赖。

不需要重装依赖。`build_and_test.sh` 和诊断脚本会自动加载 `scripts/use_local_deps.sh`，只影响本次子进程。单独运行 C++ 二进制或 CTest 时，在子 Shell 中先 source 此脚本。

首次迁移到其他服务器时，先检查依赖；共享服务器不要直接运行 apt/dnf 安装或替换系统库。完整依赖清单见 README。构建目录和安装前缀不构成权限隔离，优先普通开发账号。

## 构建、测试、保存日志

```bash
(
  cd /home/agent_proxy/sandbox_benchmark/agent_proxy || exit 1
  export JOBS=1 CTEST_PARALLEL_LEVEL=1
  export BRPC_ROOT="$PWD/.deps/brpc-install" BUILD_DIR="$PWD/build"
  export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}"
  export no_proxy="127.0.0.1,localhost${no_proxy:+,$no_proxy}"
  nice -n 10 bash scripts/validate_linux.sh
  result=$?
  printf '\nValidation exit code: %s\n' "$result"
  exit "$result"
)
```

正常结果：core、mock-protocol、e2e 全部通过且退出码 0。此次迁移会重新生成 Protobuf C++ 代码，并增量重编译，不需要清空 .deps 或 build。

e2e 仅访问它启动的本机 Mock，自动选择临时端口、清理自己的进程。无需 E2B 凭据、无需提前启动 Mock 或 Proxy。

## 无法传文件时，粘贴终端文字

请先贴 git 提交号、首个编译 error 附近的上下文或完整 CTest 错误、退出码。编译失败不要只贴最后的 make Error 2。

E2E 失败时，可打印短摘要：

```bash
python3 - <<'PY'
import json
from pathlib import Path
p = Path('build/test-results/e2e.json')
if p.exists():
    r = json.loads(p.read_text())
    print('passed:', r.get('passed'))
    print('cases:', len(r.get('cases', [])))
    print(r.get('error', ''))
else:
    print('No E2E report generated')
PY
```

如果本次编译失败，旧 e2e.json 不能证明本次通过。日志压缩包会继续保留在服务器，便于本地排查，不要求传出。

只重跑已构建的测试：

```bash
(
  cd /home/agent_proxy/sandbox_benchmark/agent_proxy || exit 1
  source scripts/use_local_deps.sh
  nice -n 10 ctest --test-dir build --output-on-failure -j 1
)
```

诊断包包含 CMake 配置、编译日志、Mock 测试请求/响应，不打包整个 .deps、环境变量值或真实后端配置。真实 E2B 日志可能包含自己的请求标识；不要将凭据或业务数据粘贴出来。

真实 E2B 的只读探测与独立沙箱冒烟测试见 `e2b-validation.md`。
