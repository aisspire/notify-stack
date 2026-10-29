# NapCat 掉线告警与二维码

项目源码在本地维护，服务器部署路径通常建议为 `~/services/notify-stack`。所有源码、配置示例、测试和文档随仓库管理。

检测器是独立的 `napcat-watch` 容器，通过现有 NapCat WebUI API 获取登录状态。不修改 NapCat 核心文件，不依赖 AstrBot 插件是否正常加载，也不挂载 Docker socket。

## 默认行为

| 项目 | 默认值 / 行为 |
| --- | --- |
| 探测间隔 | 15 秒 |
| 持续离线判定 | 60 秒；启动时已未登录也会告警 |
| 恢复判定 | 连续在线 30 秒 |
| 持续离线提醒 | 每 30 分钟一次，避免反复刷二维码 |
| 通知失败重试 | 60 秒后重试；成功回执后才进入提醒间隔 |
| 掉线优先级 | critical / Gotify 9 |
| 恢复优先级 | info / Gotify 2，通常静默 |
| 登录链接 | 1 小时有效，恢复在线后撤销整次事件的旧链接 |
| 刷新二维码 | 手动刷新有 30 秒冷却；不会对在线、扫码确认或初始化中的账号发起刷新 |

容器或 WebUI 无法连接时，告警会明确写“无法读取登录状态”，不把接口故障伪装成确定的 QQ 被踢原因。短暂断开会被 60 秒阈值过滤。网络不可达与未登录交替发生仍按持续异常计时。

告警通过统一网关发送。正文第一行提供“打开操作页面”链接，后面是告警文字和二维码图片；同时配置 Android 通知栏的点击地址和大图。应用内应点击正文链接，点击整条消息卡片不一定会打开网页。图片显示受客户端加载影响时，仍可通过正文链接进入登录页。

## 手机使用

1. 收到“QQ 已离线或未登录”后，打开 Gotify 查看**最新一条**告警。
2. 点击正文第一行的“打开操作页面”，在浏览器中查看二维码，使用手机 QQ 扫码并确认。直接点击系统通知也可打开登录页；展开通知可能显示二维码大图。
3. 同一部手机可以尝试保存图片，从 QQ 扫码相册识别；如果 QQ 限制相册中的登录码，可在电脑 Gotify 网页中打开该消息/登录页，再用手机 QQ 扫码。
4. 二维码过期时点击登录页的“刷新二维码”。不会自动在每次浏览页面时重置二维码，也不会频繁追加消息。
5. 连续在线 30 秒后收到恢复通知。旧登录入口和图片链接随即失效。

通知中的图片是发送时的二维码，可能比 1 小时链接更早过期；届时打开操作链接并刷新即可。登录页每 15 秒更新状态，不自动刷新二维码。旧图片版本在服务端更新后返回 410，防止把缓存旧图误当成新码。

如果消息没有链接，请确认查看的是部署更新后发送的新告警，旧消息不会自动更新。也可在浏览器访问你的 Gotify 域名（如 `https://gotify.example.com`），使用同一普通接收账号查看最新消息，再点击正文链接。若网页正常而 App 仍不显示链接，请记录 App 版本以便进一步定位。

如果刷新曾提示“请求来源无效”，请从最新告警重新打开登录页，或重新加载页面后再点“刷新二维码”。旧页面曾使用 `no-referrer`，会使浏览器表单提交的 `Origin` 变成 `null` 并触发拦截；当前策略改为 `strict-origin`，只传来源站点、不包含签名路径。服务端仍拒绝 `null` 和其他站点的来源，不需要调整手机权限。

## 资源限制

| 容器 | 内存上限 | 内存保留 | 内存 + swap 上限 | CPU 上限 |
| --- | --- | --- | --- | --- |
| napcat-watch | 64 MiB | 24 MiB | 96 MiB | 0.15 核 |
| gateway | 96 MiB | 32 MiB | 128 MiB | 0.5 核 |
| gotify | 96 MiB | 24 MiB | 128 MiB | 0.5 核 |
| cloudflared | 64 MiB | 24 MiB | 96 MiB | 0.25 核 |

四个通知服务的物理内存上限合计 320 MiB，实际占用见验证记录；它们的 swap 另有独立上限。此表不包含原 AstrBot、NapCat 等其他容器。检测器单进程、HTTP 连接池最多 4 个连接、只缓存当前一张 PNG、不使用 Pillow/浏览器/数据库/消息队列。

网关和检测器使用小型 socket HTTP 健康探针，避免每次健康检查导入 SSL/urllib，降低 0.15 核限制下的探针启动开销。检测器健康检查有 20 秒启动宽限和 5 秒超时。

## 配置与部署

网关与 Gotify 先按 README 部署。在服务器以现有 root SSH 账户执行：

```bash
cd /root/services/notify-stack
python3 scripts/prepare_napcat.py --configure-tunnel
docker compose config --quiet
docker compose build gateway
docker compose up -d --wait gotify gateway napcat-watch
docker compose up -d --force-recreate --wait cloudflared
```

准备脚本仅读取原 NapCat 的 `webui.json`，将 token 复制到本项目受保护的独立文件，并生成单独的链接签名密钥；不更改原 NapCat 配置。`.env`、隧道配置会先备份。脚本不安装系统软件或直接启动容器。

`.env` 通过 `COMPOSE_FILE=docker-compose.yml:docker-compose.napcat.yml` 载入可选覆盖文件。检测器加入 `notify-stack_notify` 和现有 `astrbot_default` 网络，在 Docker 内访问 `http://napcat:6099`；其他部署可通过准备脚本参数指定网络和原 WebUI 配置路径。

可调整环境变量：`NAPCAT_POLL_SECONDS`、`NAPCAT_OFFLINE_SECONDS`、`NAPCAT_RECOVERY_SECONDS`、`NAPCAT_REMINDER_SECONDS`、`NAPCAT_LINK_TTL_SECONDS`。修改后重建 `napcat-watch` 使新环境生效，不要只 restart。

Cloudflare 同一主机名下，`^/napcat/.*` 路径优先转发到 `http://napcat-watch:8091`，其余路径继续转发 Gotify。NapCat 管理接口仍只在宿主机 loopback 和既有 Docker 网络可达，未公开到公网。

## 检查与补发

```bash
cd /root/services/notify-stack
docker compose ps
docker compose logs --tail 50 napcat-watch
docker stats --no-stream notify-stack-napcat-watch-1
```

通过容器环境中的凭据查看状态，无需将密钥复制到终端历史：

```bash
docker compose exec -T napcat-watch python - <<'PY'
import json, os, urllib.request
req = urllib.request.Request(
    'http://127.0.0.1:8091/internal/status',
    headers={'Authorization': 'Bearer ' + os.environ['GATEWAY_API_TOKEN']},
)
with urllib.request.urlopen(req, timeout=10) as response:
    print(json.dumps(json.load(response), ensure_ascii=False))
PY
```

必要时将上面的路径改成 `/internal/notify`，并给 Request 增加 `data=b''`，可补发当前告警和新二维码。补发至少间隔 60 秒，账号在线时不发送掉线通知。内部接口未通过隧道公开。

原 NapCat WebUI token 更换后，重新执行准备脚本复制最新 token，再重建检测器。链接签名密钥默认保留；主动更换它会使所有旧链接失效。

## 状态与安全边界

- `napcat-watch-data` 命名卷仅保存异常事件 ID、通知时间及待发送恢复状态，不保存 QR 登录票据、完整链接或 API Token。重启后保留告警间隔，避免重复刷屏。
- QR 图片在检测器中生成，不调用第三方二维码服务，也不将 QQ 登录票据写到日志。
- 操作链接有签名、有效期和事件绑定；属于可访问此次扫码页面的凭据，不应转发到公开群。页面设置 no-store、strict-origin，不在 Referer 中传递签名路径，不加载外部资源。
- 旧链接在恢复判定后撤销；在此之前，图片和刷新接口也会实时检查在线状态，在线即拒绝提供旧二维码。
- 刷新失败仍先发文字告警。不会强制重启 NapCat 或登出 QQ；原 QQ 内核无法重建登录服务时，仍可能需要通过原管理面板处理。
- 网关 V1 不提供持久化幂等去重；检测器失败重试属于尽力投递，超时之后可能重复，不能宣称恰好一次。
- 同一服务器整机宕机依然需要独立外部心跳告警。

## 验证

本地运行 `python -m unittest discover -s tests -v`，测试覆盖持续异常计时、冷启动未登录、恢复通知与撤销、重启保持间隔、会话缓存与鉴权退避、延迟 QR 更新、链接过期/篡改、图片版本、刷新冷却和发送失败重试。

不应为演示而踢掉正在在线的 QQ。当前已未登录时可直接验收真实二维码；恢复逻辑由单元测试先验证，再通过真实扫码上线观察。
