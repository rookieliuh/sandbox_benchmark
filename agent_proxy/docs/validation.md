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

新版 Linux C++ 编译与 CTest 尚未执行，不声称新版 E2E 已通过。用户不能传出服务器文件，可粘贴编译器首个 error 附近输出、CTest 输出和 E2E 摘要，继续定位。

## 真实后端

未调用现有 E2B/KASandbox 部署。健康检查、控制 API、数据面、模板实际规格、全内存暂停、TTL、证书和权限需按 `e2b-validation.md` 单独验证。Mock 不证明真实部署兼容性或性能。
