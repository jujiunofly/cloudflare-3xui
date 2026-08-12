# Cloudflare IP → 3x-ui shareAddr 同步

每轮都以 Playwright discover Cloudflare 页面当前 XHR API，再将第一条优选 IP 更新到 3x-ui：`cucc` 对应联通，`cmcc` 对应移动，`ctcc` 对应电信。接口不写回配置文件。

## 每日运行区间与通知

在 `config.json` 编辑：

```json
"schedule": { "enabled": true, "start": "08:00", "end": "23:30" }
```

采用本地时区；支持跨日区间，例如 `22:00` 至 `06:00`。非运行时间不采集也不修改面板。Telegram 成功和失败通知均带更新时间、今日次数、成功次数、失败次数。

## 推荐 Docker Compose 部署

让 Git 代码、配置和输出分离：

```bash
mkdir -p /opt/cloudflare-3xui
cd /opt/cloudflare-3xui
git clone https://github.com/YOUR_GITHUB_USER/cloudflare-3xui.git app
cp app/docker-compose.server.yml docker-compose.yml
cp app/config.example.json config.json
nano config.json
touch cloudflare_ip.json
docker compose up -d --build
docker compose logs -f
```

目录：

```text
/opt/cloudflare-3xui/
├── docker-compose.yml
├── config.json
├── cloudflare_ip.json
└── app/
```

更新代码：

```bash
cd /opt/cloudflare-3xui/app && git pull
cd .. && docker compose up -d --build
```
