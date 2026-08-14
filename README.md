# Cloudflare IP → 3x-ui shareAddr 同步

仓库：https://github.com/jujiunofly/cloudflare-3xui

每轮都以 Playwright discover Cloudflare 页面当前 XHR API，再将第一条优选 IP 更新到 3x-ui：

| remark 关键字（不区分大小写） | 线路 |
| --- | --- |
| `cucc` | 联通 |
| `cmcc` | 移动 |
| `ctcc` | 电信 |
| `mix` | 多线 |

接口地址不写回配置文件。节点的「暂停 / 锁定」策略写在可写文件 `node_state.json`。

## 每日运行区间与通知

在 `config.json` 编辑：

```json
"schedule": { "enabled": true, "start": "08:00", "end": "23:30" },
"telegram": {
  "enabled": true,
  "bot_token": "123456:ABC...",
  "chat_id": "你的数字 chat_id",
  "notify_on_success": false,
  "notify_on_failure": true,
  "notify_on_start": true,
  "notify_on_rest": true
}
```

采用本地时区（Compose 默认 `Asia/Shanghai`）。支持跨日区间，例如 `22:00` 至 `06:00`。非运行时间不采集也不修改面板。

- **工作开始**（进入窗口）：发送「工作时段开始」通知  
- **进入休息**（离开窗口）：发送「进入休息时段」通知  
- 成功 / 失败通知可按上面开关控制  

休息时段仍可用机器人查看节点并手动锁定 / 解锁。

## Telegram 机器人设置

### 1. 创建机器人

1. 在 Telegram 搜索 `@BotFather`
2. 发送 `/newbot`，按提示起名
3. 复制得到的 **HTTP API Token**，填到 `config.json` 的 `telegram.bot_token`

### 2. 拿到自己的 chat_id

任选一种：

- 给 `@userinfobot` 发任意消息，它会回复你的数字 ID  
- 或先给自己的机器人发一条 `/start`，再浏览器打开：  
  `https://api.telegram.org/bot<你的Token>/getUpdates`  
  在 JSON 里找 `"chat":{"id": 数字}`

把该数字填到 `telegram.chat_id`（必须是字符串或数字均可，程序会转成字符串比对）。**只有这个 chat 能操作节点**，其他人发消息会被忽略。

### 3. 打开机器人功能

```json
"telegram": {
  "enabled": true,
  "bot_token": "...",
  "chat_id": "123456789",
  "notify_on_start": true,
  "notify_on_rest": true,
  "notify_on_failure": true
}
```

保存后重启容器 / 进程。

### 4. 使用方式

给机器人发送：

| 操作 | 命令 / 按钮 |
| --- | --- |
| 打开菜单 | `/start` |
| 节点列表 | `/nodes` 或按钮 **节点列表** |
| 运行状态 | `/status` 或按钮 **运行状态** |

在节点列表里点某个节点，可以：

- **恢复自动更新**：本轮起重新参与优选 IP 同步  
- **暂停自动更新**：保留当前地址，跳过自动同步  
- **锁定为固定 IP**：按提示发送 IP/域名，立刻写入 3x-ui，并不再自动改  
- **解除锁定**：回到自动更新  

策略持久化在 `node_state.json`（Docker 需挂载，见下）。

> 若曾用同一 Token 接过 webhook，需先删除：  
> `https://api.telegram.org/bot<Token>/deleteWebhook`  
> 本项目使用 long polling（`getUpdates`）。

## 推荐 Docker Compose 部署

```bash
mkdir -p /opt/cloudflare-3xui
cd /opt/cloudflare-3xui
git clone https://github.com/jujiunofly/cloudflare-3xui.git app
cp app/docker-compose.server.yml docker-compose.yml
cp app/config.example.json config.json
nano config.json
touch cloudflare_ip.json
echo '{"schema_version":1,"inbounds":{}}' > node_state.json
docker compose up -d --build
docker compose logs -f
```

目录：

```text
/opt/cloudflare-3xui/
├── docker-compose.yml
├── config.json
├── cloudflare_ip.json
├── node_state.json
└── app/
```

更新代码：

```bash
cd /opt/cloudflare-3xui/app && git pull
cd .. && docker compose up -d --build
```
