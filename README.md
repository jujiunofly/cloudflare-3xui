# Cloudflare IP → 3x-ui

https://github.com/jujiunofly/cloudflare-3xui

自动发现优选 IP，更新 3x-ui 入站 `shareAddr`。

| remark 含 | 线路 |
| --- | --- |
| cucc | 联通 |
| cmcc | 移动 |
| ctcc | 电信 |
| mix | 多线 |

## 功能

1. 定时同步（可配工作时段）
2. **开始工作 / 进入休息** Telegram 通知
3. 可选：成功 / 失败通知（`config.json` 开关）

当前**没有**机器人交互（节点列表、按钮等已移除）。

## Telegram 通知

```json
"telegram": {
  "enabled": true,
  "bot_token": "BOT_TOKEN",
  "chat_id": "你的数字ID",
  "notify_on_success": false,
  "notify_on_failure": true,
  "notify_on_start": true,
  "notify_on_rest": true
}
```

1. `@BotFather` 建 bot，拿到 token  
2. 用 `@userinfobot` 或 `getUpdates` 拿到 `chat_id`  
3. `schedule.enabled: true` 时才会发开始/休息通知  

## Docker

```bash
mkdir -p /opt/cloudflare-3xui && cd /opt/cloudflare-3xui
git clone https://github.com/jujiunofly/cloudflare-3xui.git app
cp app/docker-compose.server.yml docker-compose.yml
cp app/config.example.json config.json
nano config.json
touch cloudflare_ip.json
echo '{"schema_version":1,"inbounds":{},"telegram":{}}' > node_state.json
docker compose up -d --build
```

更新：

```bash
cd /opt/cloudflare-3xui/app && git pull
cd .. && docker compose up -d --build
```
