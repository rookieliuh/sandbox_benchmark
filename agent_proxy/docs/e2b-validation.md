# 共享服务器上的真实 E2B 验证

验证分三层：部署线索、控制面 API、独立沙箱生命周期。安装了 E2B SDK/命令行不等于本机部署了可用后端。先完成新协议 Mock 测试，再执行有副作用的真实沙箱测试。

## 1. 只读确认部署线索

以下命令不重启服务、不安装软件、不输出进程命令行或环境变量：

```bash
systemctl list-units --type=service --all --no-pager | grep -Ei 'e2b|sandbox|orchestrat|nomad|kube|containerd|docker'
ss -lnt
command -v docker
command -v kubectl
```

某条没有输出只表示没有找到线索。不要据此删除/安装组件或扫描其他主机。如果是已获授权的 Docker 部署，可读取服务摘要：

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
```

如果是 Kubernetes，先确认集群上下文，再仅查询部署方给出的命名空间：

```bash
kubectl config current-context
kubectl -n <实际命名空间> get pods,svc,ingress
```

不用 `docker inspect`、`kubectl get secret` 或导出所有环境变量排查；这些可能包含密钥。最终需要向部署方拿到：

- 控制面 API 根地址（不是 envd 地址、Dashboard 或 Kubernetes API）。
- 可用于创建独立测试沙箱的 API Key，留在服务器，不回传。
- 经核实的模板 ID、vCPU 数量和内存 MiB。
- 数据面域名或网关 URL、TLS 信任要求，以及该模板是否支持完整暂停恢复。

参考库 KASandbox 的 API/网关/编排组件可能运行于其他节点；当前服务器不一定有本地 systemd 服务。

## 2. 只读 API 探测

确认地址后替换下面占位值：

```bash
cd /home/agent_proxy/sandbox_benchmark/agent_proxy
python3 scripts/check_e2b.py --api-url 'https://实际控制面地址'
```

脚本先 GET `/health`，再读取 `E2B_API_KEY`（未设置则交互隐藏输入）并 GET `/sandboxes?limit=1`。不发送 POST/DELETE，不展示密钥、沙箱列表、token 或原始响应体，不跟随重定向。使用系统 TLS 验证，不跳过证书检查。

- health=200：仅证明该端点响应，不能证明 E2B 可创建沙箱。
- sandbox list=200 且结构匹配：证明控制 API 可达、读取权限可用，仍不证明创建权限或 envd 可达。
- 401/403：检查密钥/权限。
- 404：检查根地址、反向代理路径及 API 版本；不要直接认定“没有沙箱”。
- 超时/连接错误：检查指定地址的网络和监听情况。
- TLS 错误：向部署方取得正确证书链/访问域名，不使用 `-k` 绕过。

空列表也是合法成功。若部署不支持这个列表 API，脚本可能失败，需要按该部署的 API 契约核对；不自动降级尝试有副作用的请求。

## 3. 准备项目专用配置

复制 `configs/e2b.example.json` 为 `configs/e2b.local.json`，仅保留一个需要的后端。`.local.json` 已被 git 忽略。示例（所有地址、模板和规格都必须由部署方核实）：

```json
{
  "backends": {
    "e2b-test": {
      "type": "e2b",
      "profile": "kasandbox",
      "api_url": "https://实际控制面地址",
      "api_key_env": "E2B_API_KEY",
      "sandbox_domain": "实际数据面域名",
      "envd_scheme": "https",
      "templates": {
        "benchmark-base": {
          "backend_template_id": "实际模板ID",
          "cpu_count": 1,
          "memory_mib": 512
        }
      }
    }
  }
}
```

`profile` 可选 kasandbox/e2b_cloud，当前使用同一 REST/Connect 适配契约，不代表云端已经实测。

若有固定网关，可按部署要求使用 `envd_url_pattern`（允许 `{sandbox_id}`），否则构造 `envd_scheme://49983-{id}.{domain}`。domain 优先取后端响应，再取 sandbox_domain。使用 `X-Access-Token`、`E2b-Sandbox-Id`、`E2b-Sandbox-Port`；envd 稳定版本要求 >=0.3.0，>=0.5.7 用 octet-stream 上传，旧版用 multipart。

这里的 cpu_count/memory_mib 是模板已配置的资源，不是让 Proxy 自动改模板。不能随意填 1/512 来绕过校验。

## 4. 用一个独立沙箱测试六个 RPC

### 基础六接口 + 独立 SpDiag 会话（当前建议）

先推送本地代码并在服务器更新。复用此前已验证过创建/命令执行的项目专用 `.local.json`；
不要把示例域名或猜测的 `127.0.0.1:3002` 替换进已有配置。
本模式按 CreateSandbox → GetStatus → RunStep（`printf hello`）→ PauseSandbox →
ResumeSandbox → DeleteSandbox 执行，每种 RPC 正常时一次。暂不执行文件读写、编辑、
暂停后的文件保留或内存恢复检查，也不注入错误/延时。

```bash
(
  set -euo pipefail
  cd /home/agent_proxy/sandbox_benchmark/agent_proxy
  git pull --ff-only origin main
  source scripts/use_local_deps.sh
  printf '已有 E2B 配置文件：\n'
  printf '%s\n' configs/e2b*.local.json
  read -r -p '输入之前验证过的配置路径（configs/...local.json）：' ap_e2b_config
  test -f "$ap_e2b_config"

  # 与此前 --noproxy 探测一致；只影响本次子 Shell 及其子进程。
  export NO_PROXY='*' no_proxy='*'
  mkdir -p validation-logs
  ap_log="validation-logs/e2b-basic-spdiag-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
  set +e
  nice -n 10 python3 -u -B scripts/validate_spdiag.py \
    --build-dir "$PWD/build-spdiag" \
    --e2b-config "$ap_e2b_config" --allow-create 2>&1 | tee "$ap_log"
  ap_rc=${PIPESTATUS[0]}
  printf '\n真实 E2B 测试退出码：%s\n完整日志：%s\n' "$ap_rc" "$ap_log"
  exit "$ap_rc"
)
```

配置只有一个后端及模板时自动选中，并读取其 CPU/内存和 `api_key_env`；若有多个，
脚本在创建资源前停止，需在命令中明确加 `--backend <配置中的名称>`、
`--template <配置中的名称>`。它们是本地配置别名，不一定是模板 ID。
API Key 从该环境变量读取，缺少时在终端隐藏输入，不写入配置、报告或命令行。
已确认的模板 `9wlppnd4mfvtb6m80qlr` 规格为 1 vCPU / 1024 MiB；以此前核验且保存在
本地配置中的规格为准，不修改服务端模板。

当前命令只需要现有 `build-spdiag` 二进制和 `.deps/spdiag`，无需重新编译或安装依赖。
会实际占用一个测试沙箱的配额，TTL 为 300 秒，并主动删除本次创建的沙箱。
TTL 是后端超时设置，不能代替清理确认。失败时可能增加用于清理的 GetStatus 调用，
不会重试创建或操作已有的其他沙箱。宿主系统配置、其他服务和 SpDiag 默认会话不变。

终端逐步显示 `ERROR_*` 与 `proxy_elapsed_ms`。六接口功能、控制面清理确认和 SpDiag
计数通过才打印本模式 PASS；SpDiag 耗时仅展示原始值，明确标记 **unvalidated**，
不将已知的长区间计时问题与 E2B 功能成败混为一项。可先用代理原有 steady_clock 计时
观察实际流程耗时。原来的 Mock 严格耗时校验保持不变。

请回传每个 RPC 的状态/耗时、SpDiag 表格、`Proxy shutdown`、`Run ID`、
`Create request ID` 和最后结果。`Real sandbox cleanup confirmed` 表示沙箱清理确认，
末尾 `cleanup_confirmed` 表示本次 SpDiag SHM 清理确认。若沙箱清理未确认，先保留
请求 ID 并核对本次资源，不反复执行创建。不要发送 Key、token 或整份配置。

### 包含文件工具的完整验证

继续使用独立 SpDiag 采集时，在真实模式增加 `--e2b-file-tools`：

```bash
(
  set -euo pipefail
  cd /home/agent_proxy/sandbox_benchmark/agent_proxy
  git pull --ff-only origin main
  source scripts/use_local_deps.sh
  export NO_PROXY='*' no_proxy='*'
  mkdir -p validation-logs
  ap_log="validation-logs/e2b-files-spdiag-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
  set +e
  nice -n 10 python3 -u -B scripts/validate_spdiag.py \
    --build-dir "$PWD/build-spdiag" \
    --e2b-config configs/e2b-smoke-20261012T195909-3371445.local.json \
    --backend e2b-test --template benchmark-base \
    --e2b-file-tools --allow-create 2>&1 | tee "$ap_log"
  ap_rc=${PIPESTATUS[0]}
  printf '\n文件工具验证退出码：%s\n完整日志：%s\n' "$ap_rc" "$ap_log"
  exit "$ap_rc"
)
```

这里的配置路径是本次已核验服务器上的文件，其他部署需选择自己的已验证配置。
复用现有二进制和 RPM，无需重新编译或修改系统。只创建一个测试沙箱，所有文件操作均位于
该沙箱内的 `/tmp/smoke-<本次随机ID>.txt`，不会读写宿主机或其他沙箱的文件。
写入 `agent proxy smoke` 后读回，再编辑为 `agent benchmark smoke` 并读回；暂停恢复后
再次核对内容。阶段输出为 `File check [write-read/edit-read/after-resume]: PASS`，
报告的 `file_checks` 分别保存验证结果。仅工具返回成功但内容不符也会失败。

正常计数为 Create/Pause/Resume/Delete 各 1，RunStep 为 6（bash、write、read、edit、read、read），
GetStatus 为 3（就绪查询、创建记录查询、清理前确认），共 13 次，Bad/Not 均为 0。
SpDiag 仍按 RPC 聚合，RunStep 六次调用不会产生六个独立 SpDiag 点位；每次工具的代理耗时
可从终端和报告的 `perf_cases`（含 tool）查看。SDK 耗时继续标记 unvalidated。
最终 PASS 要求内容检查、生命周期、沙箱清理、代理正常退出和 SpDiag 计数均通过。

失败时贴回三个阶段附近的输出、`Proxy error`、清理结果、请求 ID 和最终状态即可；
不用上传文件或粘贴 Key。未确认清理前不要连续重跑创建。

若只验证功能、不采集 SpDiag，可直接使用下面原有 smoke 命令：

以下会实际创建资源、执行命令、读写自己的临时文件、暂停、恢复、删除，需要测试资源配额。不要将其他人的 sandbox ID 填入脚本；脚本没有接受已有 ID 的参数。

```bash
(
  set -euo pipefail
  cd /home/agent_proxy/sandbox_benchmark/agent_proxy
  source scripts/use_local_deps.sh
  read -r -s -p 'E2B API key: ' E2B_API_KEY
  printf '\n'
  export E2B_API_KEY
  python3 scripts/e2b_smoke.py \
    --backends configs/e2b.local.json \
    --backend e2b-test --template benchmark-base \
    --cpu 1 --memory-mib 512 --ttl 300 --allow-create
)
```

同样替换 CPU/内存为实际模板规格。这个命令不会修改现有 E2B 部署；会启动自己的 loopback Proxy 子进程，只操作自己新建的沙箱。仍会消耗配额和资源。`--allow-create` 是明确选择创建测试资源，不是任何部署/系统变更授权。

依次检查：Create READY、GetStatus、bash 输出、write/edit/read、Pause PAUSED、Resume READY、文件保留、操作记录查询。finally 尝试按本次创建请求 ID 删除，然后只停止自己启动的 Proxy 进程。每次使用唯一 request_id，不自动重发创建。

PASS 需要操作成功且 cleanup_confirmed=true。这里只确认控制面已不存在已知沙箱，不证明宿主资源物理回收；文件保留也不证明全内存暂停正确。内存快照需另行做内存进程状态验证。

若提示 CLEANUP UNCONFIRMED，保留打印的 Create request ID，请部署方按沙箱 metadata.agent_proxy_request 核对。创建响应丢失时，当前实现不能自动找到未知 ID 的资源，不能保证清理；不要盲目重复创建或删除无关沙箱。

## 5. 只回传终端文字

无需上传任何服务器文件。先贴只读检查的状态码和异常类型；真实测试贴每一步 ERROR_*、Run ID、Create request ID 和最后 PASS/FAIL。不要贴 API Key、envdAccessToken 或整份真实配置。

Mock 测试失败时可以打印本地报告的摘要：

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
    print('No E2E report: build or startup may have failed earlier.')
PY
```

真实测试报错优先贴脚本标准输出；不要直接把可能含业务信息的日志全文发送出来。

## 6. 创建成功后仍等待就绪的诊断

CreateSandbox 的 RPC 时间预算包括 POST /sandboxes 和随后访问 envd /health 的时间。
TTL 是沙箱存活时间，与 RPC 等待时间不同。控制面创建成功不保证数据面可用。
envd 的 GET /health 正常响应是 HTTP 204（无正文）；适配器接受 204，并兼容 200。
不要把控制面 /health 的 200 响应要求套用到沙箱内 envd。Mock 默认健康响应也使用 204。

Proxy 的每条操作汇总日志包含 envd_health：attempts、last_http_status、
last_transport_code 和 http_counts。只记录状态码/次数，不记录响应正文、URL、密钥或访问令牌。
HTTP 状态为 0 表示最后一次检查没有取得完整 HTTP 响应；CURL_N 是 libcurl 错误编号，
AP_N 是 Proxy 内部错误编号。就绪超时也会在 RPC error.message 中携带最后一次检查结果，
冒烟脚本直接打印该错误及独立的 Cleanup confirmed 字段。

首次就绪检查立即发出，失败后间隔 250ms、500ms，之后最多每秒一次，受原 RPC 截止时间约束。
这只改变 GET /health 的重试频率，不重试创建请求，不增加原有超时，也不修改共享后端配置。
HTTP 404 可能涉及沙箱路由/注册，502/503/504 可能涉及上游可用性；不能仅凭一个状态码确定根因。
删除成功后的健康检查会失去原有诊断价值，不要用已删除沙箱的 404 反推创建时的状态。

所有非成功 HTTP 响应在 error.backend_code 中记录 HTTP_N，在 error.message 中保留
source（control/envd）、phase、method、resource 和 http_status；不输出原始响应正文或请求 URL。
例如 envd / operation / GET / file 表示文件读取被拒绝，而 envd / operation / POST / file
表示上传失败。冒烟脚本按 RunStep[bash/write/read/edit] 打印工具名，先独立验证写入后的读取，
再验证编辑和编辑后的读取。根据具体状态排查，不从 ERROR_BACKEND_ERROR 推断某一种根因。
