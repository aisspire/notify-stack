# 架构审查

审查日期：2026-10-29。结论：作为个人 Android 统一通知入口，这个方案合理；相比依赖第三方微信推送，需要自行维护服务和手机后台连接，但能够统一 API、保留历史，并控制后续渠道迁移。对于只解决一次 NapCat 告警，Server酱接入成本更低。

## 需要修正和明确的约定

1. **连接方向。** 业务调用网关，网关在 Docker 网络中写入 Gotify；Android 主动向域名建立 HTTPS/WSS 连接，经 Cloudflare Tunnel 到达 Gotify。Gotify 不会向手机主动建立 TCP 连接。Cloudflare 支持 WebSocket，但维护、空闲超时和网络变化仍会中断连接，必须验证客户端重连。[Cloudflare WebSocket 文档](https://developers.cloudflare.com/network/websockets/)
2. **容器中的 localhost。** 如果已有 cloudflared 在另一个容器里，`http://127.0.0.1:8080` 指向 cloudflared 自己。让它加入本项目网络并使用 `http://gotify:80`；网络附件应写回它自己的 Compose，避免重建后丢失。
3. **Gotify 用户与应用归属。** 应用消息只有创建应用的用户可见。推荐管理员仅用于管理，另建普通通知用户；在该用户下创建应用，并用同一用户登录 Android。网关应用 Token、客户端 Token、管理员密码和业务 API Token 是不同凭据。Gotify 3 的应用 Token 仅创建或轮换时显示一次。[官方推送说明](https://gotify.net/docs/pushmsg)
4. **成功与重试。** V1 返回 200 只表示收到 Gotify 的消息 ID，不表示手机展示、响铃或用户已读。504、连接中断和无效回执可能发生在 Gotify 已写入之后。V1 无持久化去重，不自动重试；调用方重试也可能造成重复通知。未来引入 SQLite 时，应先明确持久化后返回 202、幂等窗口和至少一次投递语义。
5. **去重字段不能制造误解。** V1 接受 `dedupe_key` 作为元数据，响应明确返回 `dedupe_applied=false`；相同值仍会发送两条消息。不能仅凭字段存在就宣称具备幂等性。
6. **健康检查分层。** `/health` 检查网关存活；鉴权的 `/ready` 检查 Gotify `/health`，后者包含数据库探活，但不能证明应用 Token 有效或手机在线。真实发送用于验证 Token。Docker 标记 unhealthy 本身不会触发 `unless-stopped` 重启；该策略主要处理进程退出。[Gotify 健康检查实现](https://github.com/gotify/server/blob/v3.1.1/api/health.go)
7. **初始配置有顺序。** 先启动 Gotify、创建普通用户和应用，再配置网关 Token。Gotify 默认管理员环境变量只在首次创建数据库时生效，修改 `.env` 不会重置现有密码。本项目固定 Gotify 3.1.1，使用环境变量配置；3.x 已移除 YAML 配置。[Gotify 3 迁移说明](https://gotify.net/docs/migrate-to-3)
8. **手机限制。** 2/5/9 映射合理：官方文档中 1–3 对应通知栏图标，4–7 加声音，8–10 再加振动；Android 通知渠道、勿扰模式、用户设置和厂商后台策略仍会影响实际表现，critical 不能绕过系统限制。[优先级说明](https://gotify.net/docs/priority)
9. **Cloudflare Access。** 不能默认在 Android 使用的整个域名前增加交互式 Access 登录。仅有网页认证跳转不能替代 Gotify 原生鉴权；需要额外防护时，先验证客户端兼容性，或采用私网访问。初期保留 Gotify 登录、关闭公开注册，网关只映射宿主机 loopback。
10. **多用户属于权限边界。** V1 单 Token、单 Gotify 应用意味着单个接收用户，`source` 只是调用方声明的标签，不能作为可信身份。未来多用户需要 Token 到身份、可用渠道、收件人和权限的绑定，不能仅增加 Adapter。先用 Protocol 保留渠道接口，不提前实现管理后台。

## 与原 NapCat 需求的关系

该系统负责投递，仍需要独立检测器判断 QQ 是否在线。真实环境实测时 NapCat WebUI 显示 `isLogin=false`、`loginPhase=waiting_qrcode`，同时 Docker 容器运行正常，因此只监控容器存活或 AstrBot WebSocket 连接数不足以覆盖全部 QQ 登录状态。

优先使用官方 [ncd-watch](https://github.com/NapNeko/NapCatQQ-Desktop/blob/main/docs/user/ncd-watch.md)：它通过 WebUI 探测账号状态，并支持 Webhook。项目内提供了接入统一 API 的配置示例。它的首次离线不告警、Webhook 成功只判断 HTTP 状态、二维码互助依赖另一个 OneBot 的限制必须纳入验收；网关无法补回检测器未重试的事件。

二维码由独立 NapCat 检测/重登组件负责。统一请求已增加渠道无关的 `action_url` / `image_url` 字段，由 Gotify Adapter 映射 `client::notification.click.url`、`bigImageUrl` 和 Markdown 图片。登录页使用短时签名、事件绑定及恢复撤销；NapCat 管理 Token 不出现在通知中，也不会把 `127.0.0.1` 当作手机可访问地址。[Gotify Extras](https://gotify.net/docs/msgextras)

现成 ncd-watch 的冷启动行为及二维码互助通道无法直接覆盖当前需求，因此正式部署使用小型独立 `napcat-watch`。它复用 NapCat WebUI API，支持冷启动未登录告警、二维码刷新和 Gotify 图片投递，单进程且内存上限 64 MiB；保留 ncd-watch 作为仅文字告警的替代配置。

本次不将 QQ 官方机器人设为关键告警渠道。腾讯官方文档已标注主动推送能力调整，不能依赖它无交互地随时推送故障消息。[腾讯消息 API 文档](https://github.com/tencent-connect/bot-docs/blob/main/docs/develop/api-v2/server-inter/message/send-receive/send.md)

## 可靠性边界

- V1 网关不保存消息、不排队、不保证离线补发；Gotify 自身使用持久卷保存已接受的消息。这与“网关 V2 再引入 SQLite”不冲突。
- 网关、Gotify、Tunnel、NapCat 同机部署不能自报整机断电。独立外部心跳与异地备用渠道仍有必要。
- 高优先级不等于紧急告警保证。生产验收包含锁屏、移动数据切换、Android 重启、长时间待机、Tunnel 重连以及真实手机收件。
- 本版仅将网关暴露给本机可信脚本；Docker 网络内的连接仍以 Token 鉴权。对公网开放之前再补独立凭据、限流及审计。
