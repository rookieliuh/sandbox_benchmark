# 验证记录

## 已完成的服务器基线

2026-10-12，用户在 openEuler 24.03 / aarch64 / GCC 12.3.1 / CMake 3.31.12 / Python 3.13.13 上验证提交 `81e2aa4`：core、mock-protocol、e2e 三组 CTest 全通过，耗时 6.14s。该记录属于旧 `agentproxy.v1` 协议版本，不可当成本次迁移已通过。

服务器缺少的开发依赖已在项目 `.deps/local` 内安装：Protobuf 3.14.0、LevelDB 1.23、nlohmann/json 3.11.3；bRPC 1.12.1 在 `.deps/brpc-install`。

## 本次 benchmark.agent.v1 迁移

- 协议原样采用协作方文件，六个 RPC 和字段/枚举编号保持一致。
- 本机通过 Protobuf 3.14.0 protoc 生成 C++/Python 接口和 descriptor，核对关键生成方法；未在本机编译/链接 C++。
- Python Mock 测试 14/14 通过，新增暂停 204 后仍 pausing、删除 204 后资源仍存在、只读列表无资源变更，并验证 API 探测脚本不修改资源、不泄露密钥且拒绝错误密钥。
- Python 文件通过 compileall；C++ 19 个 .cc/.h 通过 tree-sitter 语法解析和 clang-format。语法解析不检查预处理、类型或链接，不能替代实际编译。
- 新增 E2E 用例：request_id 重复/冲突、原始成功/失败响应查询、运行中/未知操作、规格不匹配、未知创建清理不可确认、按创建 ID 清理、状态确认、记录容量不足、稳定 Proxy ID、JSON 结果/null 退出码；保留四工具、非零退出、超时、输出上限、排队超时、旧上传协议等覆盖。
- E2E 还会用 Mock 运行真实 E2B 冒烟驱动，检查脚本自身的创建和清理链路；这一项不访问真实后端。

## 待服务器执行

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

用户随后在同一服务器验证协议迁移版本：core、mock-protocol、e2e 全部通过，耗时 8.71s。
这一结果不涵盖后续就绪诊断改动；诊断改动的 Linux C++ 编译与 CTest 仍待执行。
用户不能传出服务器文件，可粘贴编译器首个 error 附近输出、CTest 输出和 E2E 摘要，继续定位。

## 真实后端

2026-10-12 用户验证了现有后端的控制 API 认证和模板读取。单沙箱测试的创建控制请求约 1.27s，
随后就绪检查约 58.73s，达到 60s RPC 截止时间。清理返回成功，并由控制面查询确认资源不存在。
该次测试未进入命令、文件、暂停或恢复阶段，尚未证明完整真实链路可用。
后端 client-proxy 输出的 OpenTelemetry 日志、指标及追踪上报错误尚不能确定为就绪超时根因。

后续诊断改动新增健康检查 HTTP 状态计数、最后的传输错误，以及 250ms 至 1s 的退避；
新增 Mock E2E 覆盖 HTTP 503 与连接被关闭的区分、轮询次数上限及失败后的清理。
这些 C++ E2E 用例待服务器执行。Mock 不证明真实部署兼容性或性能。

诊断改动的本地检查：Python Mock 15/15 通过（含 HTTP 503、健康连接关闭且仍可清理的用例）；
修改的 C++ 文件通过 tree-sitter 语法解析，Python 文件通过语法检查，git diff --check 通过。
本机仍无 Linux C++ 构建环境，不能把这些检查当作编译或 C++ E2E 成功。
