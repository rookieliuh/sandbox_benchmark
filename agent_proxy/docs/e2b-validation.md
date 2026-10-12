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
