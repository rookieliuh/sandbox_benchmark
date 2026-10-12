# 验证记录

日期：2026-10-12。开发环境：Windows / PowerShell / Python 3.14，无可用 Linux、Docker、C++ 编译器。按用户安排，C++ 构建与完整链路留待上传仓库后在服务器验证。

## 已完成

- Python Mock 协议测试：9/9 通过。覆盖认证、生命周期及文件保留、stdout 和省略零退出码、非零退出、流尾错误、限流、父目录查询、信号终止、写入完成但确认连接丢失。
- `python -m compileall -q tests`：通过，覆盖 Mock 和端到端驱动的 Python 语法。
- Protobuf：用 protoc（grpcio-tools 提供）从 `proto/agent_proxy.proto` 成功生成 descriptor set，确认六个方法定义合法。
- C++ 源码：用 tree-sitter-cpp 解析 19 个 `.cc/.h` 文件，无语法树错误。**此检查不进行预处理、类型检查、符号解析或链接，不能替代 C++ 编译。**
- shell 构建入口通过 Git Bash `bash -n` 语法检查；其构建步骤未在 Windows 执行。
- 参考目录 `KASandbox` 未修改。

本机额外检查工具只安装在 `.deps/check-tools`，由 `.gitignore` 排除，不属于构建/运行依赖。生成的 descriptor set 也在 `.deps` 中。

## 待服务器执行

```bash
# 首次系统依赖见 README.md
bash scripts/build_and_test.sh
```

该命令必须真实通过以下三组 CTest 测试，才算完成首轮验收：

1. `core`：C++ 参数校验、字面替换、UTF-8/base64、Connect 拆包和错误、沙箱互斥、有界队列和排队超时。
2. `mock-protocol`：本机已通过的 Python Mock 测试在 Linux 上重跑。
3. `e2e`：启动真实 `agent-proxy-client` → bRPC → `agent-proxy` → HTTP/Connect Mock。包括六个 RPC、四种工具、正常及非零退出、暂停恢复、重复删除、参数错误、输出上限、二进制/大文件拒绝、旧版 multipart、坏流尾/缺帧、超时清理、同沙箱互斥、工作池占满后的排队超时、写入结果不确定、创建不重试、创建后健康检查超时保留 ID。

报告位于 `build/test-results/e2e.json`，同目录保留日志和每个请求响应。失败时优先提供该报告和 CTest 输出，以便继续修复。Dockerfile 也是可选验证路径，当前尚未运行。

## 尚未验证的兼容性

尚未调用真实 E2B 云端或 KASandbox 部署。特别需要实机确认：安全 token、数据面路由、envd 版本、全内存暂停、TTL、Filesystem.Stat、Connect 超时行为、上传响应。Mock 只验证协议与代理逻辑，不证明真实后端兼容性或性能。
