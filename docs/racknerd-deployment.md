# 服务器生产部署记录

部署日期：2026-10-29。

- 项目目录：`~/services/notify-stack`
- 正式地址：`https://gotify.example.com`（请按实际公网域名替换）
- 普通接收用户：`notifications`
- 手机登录信息：`.secrets/android-login.txt`，权限 600
- 网关 API：`http://127.0.0.1:8090/v1/notify`，仅绑定本机
- Gotify 本机管理接口：`http://127.0.0.1:8080`
- 隧道名称：`gotify-tunnel`
- 隧道 UUID：`replace_with_tunnel_uuid`
- Compose 使用 `COMPOSE_PROFILES=tunnel`，并通过 `COMPOSE_FILE=docker-compose.yml:docker-compose.napcat.yml` 载入监控覆盖文件，常驻运行 `gotify`、`gateway`、`cloudflared`、`napcat-watch` 四个服务。

Gotify 固定为 `3.1.1`，自建网关镜像为 `notify-gateway:1.1.0`；cloudflared 使用已核对的 `2026.10.3` 及镜像 digest。NapCat 扩展仅新增二维码编码依赖，未升级已有依赖或其他服务。

## 已完成验证

- 通过所授权的域名 DNS 路由创建隧道。
- Gotify、网关、独立 cloudflared 均通过 Docker 健康检查。
- 本机 `/health`、鉴权 `/ready` 和通知入库成功。
- 公网 TLS 证书验证通过；Cloudflare `/stream` 返回 WebSocket 101。
- 公网 WebSocket 实际收到网关写入的同一条消息 ID，而非只验证 HTTP 接受。
- 公网 `/health` 在正常浏览器 User-Agent 下返回 `health=green`、`database=green`。
- 临时连接测试客户端均在测试后删除，保留普通用户的测试消息供手机查看。

手机安装与登录步骤见 [Android 使用说明](android-usage.md)。手机通知权限、后台保活和锁屏弹出仍需真实手机验收。

部分无浏览器特征的自动化 User-Agent 会触发 Cloudflare 1010。诊断健康接口时使用实际客户端或正常测试 User-Agent；不要仅凭默认 Python urllib 的 403 判断服务不可用，也不必因此关闭整个站点的防护。

## 运维

```bash
cd ~/services/notify-stack
docker compose ps
docker compose logs --tail 100 gateway gotify cloudflared
```

普通用户密码通过受保护的 `.secrets/android-login.txt` 读取。`.env`、初始化恢复信息、账户授权证书和隧道运行凭据保存在项目受保护文件中，不提交到 Git；常驻 cloudflared 只挂载本隧道凭据，不挂载账户授权证书。

NapCat 自动告警和二维码已接入统一网关。检测器使用既有 `astrbot_default` 网络及 WebUI API，不修改原 NapCat 核心或重启其容器。独立检测器内存限制 64 MiB，其他通知服务也已收紧限制，见 [资源与运维说明](napcat-watch.md)。
