# SpDiag 六个 RPC 性能打点

本次仅接入 PerfPoint；参考仓库 `spdiag` 和原始 RPM 不修改，不需要编译 SpDiag。
Agent Proxy 的六个 RPC 都使用 `global_perf`，适配 bRPC 入口线程与工作/排队超时线程之间的切换。

## 点位与计时口径

程序名均为 `agent-proxy`，级别均为 `SUB_SYSTEM`（1），module 为 `service.cc::<对应RPC名>`。

| PerfKey | Point | 主要业务范围 |
|---|---|---|
| AGENT_PROXY_CREATE_SANDBOX_FULL | CreateSandbox | 创建及 envd 就绪等待 |
| AGENT_PROXY_RUN_STEP_FULL | RunStep | bash/read/write/edit、预检查及必要清理 |
| AGENT_PROXY_PAUSE_SANDBOX_FULL | PauseSandbox | 暂停及状态确认 |
| AGENT_PROXY_RESUME_SANDBOX_FULL | ResumeSandbox | 恢复及 envd 就绪等待 |
| AGENT_PROXY_DELETE_SANDBOX_FULL | DeleteSandbox | 删除及确认，包括已删除快速返回 |
| AGENT_PROXY_GET_STATUS_FULL | GetStatus | 操作记录查询或后端沙箱状态查询 |

Start 在 `Service::dispatch` 创建请求上下文之前；End 在公共 `complete` 完成响应、更新记录、
写入现有指标并释放沙箱操作锁之后，bRPC `done` 之前。包含排队时间，不包括调用端往返、
bRPC 接收请求前的工作、响应的网络发送、RunStep 进入 dispatch 前的少量字段设置。
后端阶段继续使用原有 queue/preflight/operation/readiness/cleanup 指标，不重复添加阶段点。

每次 RPC 独立持有打点对象，完成回调持有共享所有权。正常返回显式 End；初始化异常和
其他异常展开显式 End(ERROR_INTERNAL) 兜底，结束标记避免重复 End。不依赖 SDK 析构 Abandon。

- RPC `ERROR_OK=1` 转换为 SDK `End(0)`，计入 Good。
- 其他 RPC 错误使用原枚举数值，计入 Bad；未设置错误码按内部错误处理。
- 重复请求、校验失败、排队失败/超时同样计数；不是 Abandon。
- GetStatus 成功查询到失败的旧操作，本次查询仍为 Good。
- bash 非零退出但 RPC 成功，按 RPC 口径计 Good，命令退出码仍在结果内。
- 正常收尾的请求预期 Not/Abandon 为 0；SIGKILL、进程崩溃可能丢失在途/未刷新数据。

## 依赖准备（仅项目目录）

本次代码通过 `sandbox_benchmark` 仓库提交；`spdiag` 参考仓库不修改、不提交。
先将本地提交推送到 GitCode，再在服务器更新代码。仅本地提交而未推送时，服务器 git pull 无法取得更新。

```bash
cd /home/agent_proxy/sandbox_benchmark/agent_proxy
git pull --ff-only origin main
```

如果服务器存在导致更新失败的本地修改，保留它们并检查报错，不使用强制覆盖或 reset --hard。
源码 ZIP 可作为手动传输备选，使用 Git 更新时不需要解压 ZIP。
将下面两个原始 RPM 上传到服务器的 `agent_proxy/.deps/rpms/`：

```text
spdiag-1.0.0-1.p99_on.perflog_on.mempoint_on.obmem_on.obcache_on.aarch64.rpm
spdiag-devel-1.0.0-1.p99_on.perflog_on.mempoint_on.obmem_on.obcache_on.aarch64.rpm
```

```bash
cd /home/agent_proxy/sandbox_benchmark/agent_proxy
mkdir -p .deps/rpms
# 上传两个 RPM 后运行：
python3 -B scripts/prepare_spdiag.py --rpm-dir "$PWD/.deps/rpms"
```

准备脚本使用 Python 标准库读取已审查 RPM 的 XZ/CPIO payload，核对固定 SHA256 后解包。
不执行包脚本，不注册 RPM，不修改 `/usr`、`/etc`、系统动态库配置或 Shell 启动文件。
输出仅在 `.deps/spdiag/` 内。包中的 CMake 导出写死 `/usr`，脚本仅重定位解包副本的共享库
导出；库文件和头文件不修改。同目录库链接以内容相同的文件副本保留，便于跨平台准备验证。
准备结果带逐文件摘要；重复运行会核验，不覆盖已有修改。

CMake 使用 `find_package(SpDiag CONFIG REQUIRED)` 和 `SpDiag::spdiag_lib`，宏和 ABI 配置由
RPM 导出 target 传递。只有服务程序直接依赖 SDK；客户端和核心单元测试不增加此直接依赖。
编译需要 SDK，运行时即使关闭采集也仍需能加载 `libspdiag.so.1`。

两个包仍依赖服务器已有的 glibc/libstdc++、libbpf、libelf 等。后面的脚本先用 ldd 检查，
缺库即停止，不自动执行 dnf/yum/rpm 安装。

## 一次性构建和验证

使用已有 `.deps/local` 和 `.deps/brpc-install`，新的构建目录为 `build-spdiag`。
原来已经运行的服务不会被替换、停止或重启。

```bash
(
  set -euo pipefail
  cd /home/agent_proxy/sandbox_benchmark/agent_proxy
  nice -n 10 bash scripts/validate_spdiag_linux.sh
)
```

脚本依次执行依赖检查、CMake 配置、单并行编译、单并行 CTest，然后执行真实 SpDiag + Mock RPC 计数核对。
CTest 有 core、mock-protocol、spdiag-tools、e2e 四组。采集验证只连接随机回环端口上的临时 Mock，
不读取真实 E2B 配置、不需要真实 API Key、不创建真实沙箱。

采集脚本为每次运行创建：

- `test-results/spdiag-<随机串>/`：配置、CLI 输出、E2E 报告、CSV、最终 report.json。
- `/dev/shm/agent_proxy_<UID>_<随机UUID>`：只属于本次验证的共享内存。
- 与该会话对应的独立数据目录，不使用 `/tmp/spdiag_data`。

此 RPM 的 CLI 禁用了 `--config` 和 `--shm`。脚本在专属工作目录写入 `spdiag.conf`，
利用 CLI 的当前目录配置查找规则，所有 start/show/stop 都在同一个工作目录执行。
配置使用 16 个 slot、1 个 program、1 个 shard，足够本次六个全局点；这不是 CPU 绑核，
不限制工作线程使用 CPU。不同验证会话使用不同 SHM，避免同名程序注册相互覆盖。

SDK 在服务创建工作线程之前初始化和预热，所有请求结束并且工作线程退出后刷新最终统计。
采集脚本只停止自己创建的进程和专属 SHM；不执行全局 spdiag clear/restart/stop，也不使用 pkill。
中断会尽力清理；断电或 SIGKILL 无法保证清理。失败时保留会话目录，并明确报告 cleanup_confirmed。
所有命令仍会消耗少量 CPU、内存和磁盘，不能承诺共享服务器上完全零资源影响。

成功标志：

```text
100% tests passed, 0 tests failed out of 4
CreateSandbox: good=... bad=... abandon=0
RunStep: good=... bad=... abandon=0
PauseSandbox: good=... bad=... abandon=0
ResumeSandbox: good=... bad=... abandon=0
DeleteSandbox: good=... bad=... abandon=0
GetStatus: good=... bad=... abandon=0
PASS: SpDiag six-RPC counters and timing; cleanup_confirmed=True; report=...
SpDiag validation exit: 0
```

精确计数来自同次运行实际完成的 RPC 列表，不使用固定计数。额外容量测试实例和冒烟脚本实例
关闭采集，避免混入主测试代理的计数。覆盖六个接口、重复请求、参数/后端失败、就绪超时、
并发工具执行、排队超时、状态查询和幂等删除。计数验证等待代理正常退出后的最终刷新，
避免把后台定时刷新延迟误判为漏点。此结果不代表真实 E2B 性能，不能当生产性能结论。

采集验证另外执行一个 5 秒 Mock RunStep，并将 SpDiag 总耗时/最大耗时与代理原有 steady_clock
计时对照。检查参考源码时发现，SDK `PerfPoint::End` 使用 `(elapsed * nsPerTick_) >> 32`
进行 64 位乘法换算，长于约 4.29 秒的区间存在溢出风险；实际 RPM 的行为须由服务器测试确认。
若计数通过、耗时核对失败，报告会分别保留两项结果并提示 `tick-to-ns ... overflow`。
此时不能把统计当成有效性能数据；需要 SpDiag 维护者提供修复后的 RPM，本项目不改参考库。
新 RPM 的摘要、头文件和导出配置也需要重新核对，不能通过关闭校验来绕过。

## 后续单独重跑采集验证

```bash
(
  set -euo pipefail
  cd /home/agent_proxy/sandbox_benchmark/agent_proxy
  source scripts/use_local_deps.sh
  nice -n 10 python3 -B scripts/validate_spdiag.py --build-dir "$PWD/build-spdiag"
)
```

代理默认 `--spdiag_shm_name=`，不连接或创建任何采集共享内存。需要采集时必须显式指定
已经创建的 `/agent_proxy_*` 名称。不要将它指向其他人的会话，也不要在请求在途时删除/重建 SHM。
目前自动化脚本负责测试会话；正式 benchmark 的采集生命周期应与对应代理实例一一对应。

## 失败时粘贴哪些输出

不需要传出归档文件。请粘贴：

1. 最后退出码和报错附近文本：

   ```bash
   cd /home/agent_proxy/sandbox_benchmark/agent_proxy
   cat validation-logs/last-spdiag-exit-code.txt
   tail -n 100 "$(cat validation-logs/last-spdiag-log.txt)"
   ```

2. 若缺库：`Checking shared libraries` 下所有 `not found` 行。
3. 若编译失败：第一个编译器 `error:` 前后约 30 行，或 CMake 的完整错误段落。
4. 若 CTest 失败：失败测试名称及输出；不是只贴最终百分比。
5. 若采集验证失败：终端给出的会话目录内 `report.json`；根据错误再贴 `start.log`、`show.log`、
   `stop.log` 或 `e2e.log` 最后 100 行。`e2e.json` 内给出了具体 Mock/Proxy 日志目录。

## 本机验证范围

2026-10-13：项目内解包和重复校验通过；新增工具测试 11/11、Mock 协议测试 17/17 通过。
本机是 Windows，无 Linux aarch64 C++ 构建环境；真实编译、链接、四组 CTest 以及 SpDiag 实际
计数验证尚需在服务器执行，不能将静态检查或 Python 测试当作这些步骤已通过。
