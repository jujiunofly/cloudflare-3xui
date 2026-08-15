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
2. **开始 / 休息** 通知（完整样式）
3. 成功 / 失败通知（可关）
4. **Telegram 交互**
   - 节点列表：看状态
   - 是否参与自动更新
   - 锁定固定 IP / 解除锁定
   - 通知开关（成功·失败·开始·休息）

## config

见 `config.example.json`。Telegram 段：

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

机器人里改的开关写在 `node_state.json`，会覆盖上面默认值。

## Telegram 设置

1. `@BotFather` → `/newbot` → 复制 token → `bot_token`
2. 给机器人发一条消息，用 `@userinfobot` 或  
   `https://api.telegram.org/bot<Token>/getUpdates` 看 `chat.id` → `chat_id`
3. `enabled: true`，重启
4. 发 `/start`，用下方按钮：

| 按钮 | 作用 |
| --- | --- |
| 节点列表 | 点节点 → 参与更新 / 不参与 / 锁定 IP / 解锁 |
| 通知设置 | 开关成功、失败、开始、休息消息 |
| 运行状态 | 当前工作/休息窗 + 今日统计 |

锁定：点「锁定为固定 IP」→ 直接发 IP → 写入 3x-ui 并不再自动改。

**一个 bot token 只能跑一个容器。** 日志出现 Conflict 时：

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
