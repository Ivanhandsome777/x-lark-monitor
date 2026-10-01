# X → Lark 实时推送机器人

监控多个公开 X/Twitter 账号的新帖，并通过 Lark 群自定义机器人 Webhook 推送。使用 X API v2，支持秒级 Filtered Stream、轮询兜底、SQLite 去重、断线重连和推送失败重试。

## 可视化管理界面（推荐）

不需要手动编辑配置文件。启动后在浏览器中填写 X API Token、Lark Webhook 和要监控的账号。

```bash
docker compose up -d --build
```

打开 [http://127.0.0.1:8787](http://127.0.0.1:8787)，点击“保存并启动”。界面支持：

- “采集内容”首页：按时间查看抓回的推文、筛选账号和确认 Lark 推送状态
- “账户与统计”页：查看累计/本月费用估算、14 天趋势和各账号采集量
- 添加、删除监控账号
- 选择秒级实时流或定时轮询
- 控制是否包含回复、转帖和首次历史帖
- 测试 Lark Webhook
- 启动、停止服务并实时查看日志

费用统计按本地唯一推文数 × `$0.005` 估算，不包含用户资料、规则管理等其他 API 资源费用；最终账单以 X Developer Console 为准。

配置保存在 Docker 本地数据卷中，敏感字段权限为 `0600`，页面只返回末四位提示，不会把完整 Token 再传回浏览器。

如果本机连接 X API 出现 TLS EOF 或握手超时，在“X API 网络代理”中填写 HTTP 代理地址，例如 `http://127.0.0.1:7890`。Render 等海外服务器通常不需要此项。

直接用本机 Python 运行界面也可以：

```bash
python3 web_app.py
```

然后打开 [http://127.0.0.1:8787](http://127.0.0.1:8787)。配置和去重数据库默认保存在 `data/`。

## 创建 Lark Webhook

在目标群聊中添加“自定义机器人”，复制 Webhook 地址。如果开启了签名校验，同时保存签名密钥。

## 命令行模式（可选）

如果不使用可视化界面，也可以继续通过 `.env` 运行：

```bash
cp .env.example .env
```

编辑 `.env`，至少填写：

```dotenv
X_BEARER_TOKEN=你的_X_API_Bearer_Token
X_USERNAMES=OpenAI,elonmusk
LARK_WEBHOOK_URL=你的_Lark_Webhook
```

账号名不要带 `@`，多个账号用英文逗号分隔。

可先单独验证 Lark Webhook（不会调用 X API）：

```bash
set -a
source .env
set +a
python3 x_lark_bot.py --test-lark
```

### 启动

直接运行（只使用 Python 标准库，无需安装依赖）：

```bash
cp .env.example .env
set -a
source .env
set +a
python3 x_lark_bot.py
```

## 模式选择

- `X_MODE=stream`：Filtered Stream，通常几秒内送达，推荐。
- `X_MODE=poll`：Recent Search 轮询；用 `POLL_INTERVAL_SECONDS` 设置间隔，最低 15 秒。

若启动日志对 stream rules 或 stream endpoint 返回 `403`，说明当前 X 项目未获得该端点权限，将 `X_MODE` 改成 `poll` 后重启即可。

## 行为说明

- 默认忽略回复和转帖，可用 `INCLUDE_REPLIES=true`、`INCLUDE_RETWEETS=true` 开启。
- Stream 模式只推送连接成功后的新帖，不倒灌历史内容。
- Poll 模式首次启动默认只建立游标、不推历史；设置 `PUSH_EXISTING=true` 可推送首次查到的帖子。
- 已推送 ID 和失败待重试消息保存在 `data/monitor.db`。
- 程序只删除自己创建且 tag 以 `x-lark-monitor:v1:` 开头的 stream rules，不影响同一 X App 下的其他规则。

## 常用命令

```bash
# 查看运行状态
docker compose ps

# 查看日志
docker compose logs -f --tail=100

# 修改 .env 后重启
docker compose up -d --force-recreate
```

请遵守 X Developer Agreement、API 用量限制以及当地的数据与隐私法规。

## 部署到 Render

仓库根目录包含 `render.yaml`，可直接通过 Render Blueprint 部署：

1. 将仓库连接到 Render，选择 **New → Blueprint**。
2. Render 会识别 `render.yaml` 并创建一个 Starter Web Service、1GB Persistent Disk 和健康检查。
3. 在首次创建时设置 `ADMIN_PASSWORD`；用户名默认为 `admin`。
4. 部署完成后访问 Render 提供的 HTTPS 地址，登录管理后台并填写 X Token、账号和 Lark Webhook。

注意事项：

- 管理后台在 Render 上强制启用 HTTP Basic 登录认证。
- 配置与 SQLite 去重数据保存在 `/var/data`，重新部署不会丢失。
- 服务固定为单实例，避免同一推文重复采集和推送。
- 应用自动读取 Render 的 `$PORT`，并通过 `/api/health` 接受健康检查。
- Docker 镜像与 Render 环境都会自动监听 `0.0.0.0`；不要手动设置 `PORT`。
- `X_PROXY_URL` 在 Render 上通常保持为空。
