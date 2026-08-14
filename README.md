# Cloudflare IP → 3x-ui

https://github.com/jujiunofly/cloudflare-3xui

自动发现优选 IP，更新 3x-ui 入站 `shareAddr`。

| remark 含 | 线路 |
| --- | --- |
| cucc | 联通 |
| cmcc | 移动 |
| ctcc | 电信 |
| mix | 多线 |

## 功能（就这些）

1. **定时同步**（可配工作时段）
2. **开始/休息通知**
3. **成功/失败通知**（可关）
4. **Telegram 管理节点**
   - 是否参与自动更新
   - 锁定固定 IP / 解锁

## config 示例

```json
{
  "panel": {
    "base_url": "https://panel.example.com:2053/panel/api",
    "api_token": "TOKEN"
  },
  "runtime": {
    "discover_every_cycle": true,
    "interval_minutes": 10,
    "jitter_seconds": 45,
    "browser_wait_ms": 20000,
    "browser_timeout_ms": 30000,
    "request_timeout_seconds": 20,
    "api_retries": 3,
    "fallback_on_failure": true,
    "fallback_share_addr": "188.114.98.249"
  },
  "schedule": {
    "enabled": true,
    "start": "08:00",
    "end": "23:30"
  },
  "telegram": {
    "enabled": true,
    "bot_token": "BOT_TOKEN",
    "chat_id": "你的数字ID",
    "notify_on_success": false,
    "notify_on_failure": true,
    "notify_on_start": true,
    "notify_on_rest": true
  }
}
```

完整模板见 `config.example.json`。

## Telegram 怎么设

1. `@BotFather` → `/newbot` → 拿到 token → 填 `bot_token`
2. 给机器人发任意消息，用 `@userinfobot` 或  
   `https://api.telegram.org/bot<Token>/getUpdates` 看 `chat.id` → 填 `chat_id`
3. `enabled: true`，重启容器
4. 发 `/start`，用按钮：
   - **节点列表**：点节点 → 参与/不参与/锁定/解锁
   - **通知设置**：成功、失败、开始、休息 开关

通知开关写在 `node_state.json`（可改，不必动只读 config）。

**一个 token 只能跑一个容器。** 出现 Conflict 时：

```bash
docker compose down
docker compose up -d
```

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

`node_state.json` / `cloudflare_ip.json` 必须是**文件**，不能是目录。
