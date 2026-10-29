# Android 使用说明

部署的服务器地址：**https://gotify.example.com**（请替换为你在配置中绑定的真实公网域名）。手机需安装 Gotify Android，使用创建的普通用户（例如 `notifications`）登录。

## 安装与登录

1. 下载并安装官方 [Gotify 2.10.1 APK](https://github.com/gotify/android/releases/download/v2.10.1/Gotify.apk)，也可以在 [官方发布页](https://github.com/gotify/android/releases/latest) 选择更新版本，或从 [官方项目](https://github.com/gotify/android#installation) 提供的 F-Droid / Google Play 入口安装。至少使用 2.10.1，该版本包含 Gotify 3 兼容调整。
2. 在电脑的终端执行下面的命令读取接收用户密码。该文件只包含服务器地址、普通用户及其密码，不包含网关或应用 Token：

   ```bash
   ssh -F ~/.ssh/config -o ControlMaster=no -o ControlPath=none your-server \
     'cat ~/services/notify-stack/.secrets/android-login.txt'
   ```

3. 打开手机 App，服务器填写你的实际域名（如 `https://gotify.example.com`），用户名填写 `notifications`，密码使用上一步结果。无需填写 API Token。
4. 允许系统通知权限。确认 App 显示连接正常，并能看到此前的连接测试消息。

也可以先在电脑浏览器打开同一地址，用同一普通用户登录，确认账户和消息历史可用。不要使用管理员账户作为日常手机接收账户。

## 后台与锁屏通知

- 在系统的应用设置中允许 Gotify 后台运行、自启动，将电池使用设置为“不受限制”或关闭电池优化。
- 小米、OPPO、vivo、华为等系统可能还有独立的后台管理开关；名称因系统版本不同而不同。
- 保留 Gotify 的后台连接通知，不要在系统中强行停止 App。
- 可将“连接状态”通知渠道设为静默，保留消息通知渠道的声音/振动。
- 依次实测锁屏、待机、Wi-Fi 与移动数据切换、手机重启后的重新连接。

官方说明：开启电池优化可能导致后台进程被终止，从而无法收到实时通知。[Gotify Android 后台设置](https://github.com/gotify/android#disable-battery-optimization)

网关级别映射：`info → 2`、`warning → 5`、`critical → 9`，通常分别对应通知栏、声音、振动。最终行为受系统通知渠道及勿扰模式控制。[Gotify 优先级说明](https://gotify.net/docs/priority)

## 发送一条实时测试通知

登录完成后，让 App 保持后台并锁屏，在电脑终端执行：

```bash
ssh -F ~/.ssh/config -o ControlMaster=no -o ControlPath=none your-server \
  'cd ~/services/notify-stack && docker compose exec -T gateway python -' <<'PY'
import json
import os
import urllib.request

body = {
    "source": "phone-test",
    "title": "手机通知测试",
    "message": "如果手机锁屏后出现这条通知，实时推送已经跑通。",
    "severity": "warning",
}
req = urllib.request.Request(
    "http://127.0.0.1:8090/v1/notify",
    json.dumps(body).encode(),
    {"Content-Type": "application/json",
     "Authorization": "Bearer " + os.environ["GATEWAY_API_TOKEN"]},
)
with urllib.request.urlopen(req, timeout=10) as response:
    print(json.dumps(json.load(response), ensure_ascii=False))
PY
```

该命令在服务器网关容器中读取已配置的 Token，不需要复制真实 Token 到电脑或命令历史。成功回执表示 Gotify 已接受；手机是否弹出、响铃需要实际观察。

## 收不到通知时

1. 浏览器打不开域名：先检查手机网络、DNS 和 Tunnel。健康接口为 `https://gotify.example.com/health`，正常应返回绿色健康状态。
2. 能打开页面但登录失败：核对完整服务器地址、用户名和文件中的密码。
3. 打开 App 才看到新消息：优先检查后台限制、电池优化和 App 连接状态。
4. 有消息但没有声音：检查系统消息通知渠道、勿扰模式和消息优先级；info 级别本来就可能静默。
5. 网关命令报错：在服务器运行 `docker compose ps`，查看 `gateway`、`gotify`、`cloudflared` 日志。

NapCat 状态告警和二维码使用同一个 App。收到“QQ 已离线或未登录”后，打开 Gotify **最新一条**消息，点击正文第一行的“打开操作页面”，即可进入能刷新二维码的登录页；正文也附带二维码图片。系统通知支持点击进入登录页及展开大图，应用内请直接点击正文链接。旧消息不会自动增加新入口。同机相册识别受 QQ 版本限制时，在电脑上打开登录页再用手机 QQ 扫码。详见 [NapCat 监控与二维码说明](napcat-watch.md)。
