# Telegram → Bark 消息监听器

这个项目监听 Telegram 的**收到的私聊消息**，并通过 Bark JSON API 推送到设备。配置从环境变量或当前目录的 `.env` 读取，代码库不保存 Telegram 密钥、手机号、Bark device key 或 session 文件。

## 安装与运行

需要 Python 3.11+ 和 [`uv`](https://docs.astral.sh/uv/)。

```bash
cp .env.example .env
# 编辑 .env，填入 Telegram API 凭据和 Bark 推送地址
uv sync
uv run bark-monitor --check-config
uv run bark-monitor
```

可以先用 `uv run bark-monitor --check-config` 检查环境变量；`uv run bark-monitor --help` 查看命令选项。

默认读取当前目录的 `.env`；如果配置文件放在其他位置，可使用 `uv run bark-monitor --env-file /path/to/.env`。已经存在的进程环境变量优先于 `.env` 文件中的同名项。

首次运行会交互式输入 Telegram 验证码；登录成功后 Telethon 会在 `TELEGRAM_SESSION` 指定的位置保存 session。这个文件已被 `.gitignore` 忽略，请妥善保护。已经授权的 session 可以不设置 `TELEGRAM_PHONE`。

## 配置

`TELEGRAM_API_ID`、`TELEGRAM_API_HASH` 和 `BARK_PUSH_URL` 必填。Bark 地址应是完整的 `https://api.day.app/<device-key>` 地址；也可以使用自建 Bark 服务地址。可选的 `APP_TIMEZONE` 默认是 `Asia/Shanghai`，`BARK_TIMEOUT_SECONDS` 默认是 10 秒。

`TELEGRAM_PASSWORD` 可留空；需要二步验证时会隐藏输入，密码中的前后空格会保留。`.env` 由程序读取，无需在 shell 中 `source`，含空格或 `#` 的值请加引号。

管理面板可以通过 `uv run bark-dashboard --env-file .env` 启动，默认只监听 `127.0.0.1:8787`。如果绑定到其他地址，请设置 `BARK_DASHBOARD_TOKEN`；未设置时程序会在数据目录生成随机令牌文件，并要求所有管理 API 使用 Bearer 令牌。`/healthz` 只用于健康检查，不需要令牌。

日志默认以单行 JSON 同时输出到控制台和 `data/logs/bark-monitor.log`。可通过以下环境变量调整：

- `BARK_LOG_LEVEL`：日志级别，默认 `INFO`。
- `BARK_LOG_FILE`：日志文件完整路径；为空时使用默认路径。
- `BARK_LOG_DIR`：日志目录；仅在 `BARK_LOG_FILE` 为空时生效。
- `BARK_LOG_MAX_BYTES`：单个日志文件最大字节数，默认 `5242880`。
- `BARK_LOG_BACKUP_COUNT`：保留的轮转备份数，默认 `5`。
- `BARK_LOG_TO_FILE=false`：关闭文件日志，仅保留控制台输出。

新建日志目录权限为 `0700`，日志文件权限为 `0600`；自定义路径指向的既有目录保留原权限。URL、Bearer 令牌、手机号、环境变量中的密钥以及 Telethon 异常详情会自动脱敏；消息正文不会写入日志。管理面板的 Uvicorn 运行和错误日志使用同一套 JSON、脱敏及文件处理器；关闭逐请求访问日志，避免健康检查轮询刷屏。

## 监听行为

- 忽略机器人和自己发出的消息；群组和频道消息只记录概要，不发送 Bark 通知。
- 推送保留发送者、正文和所配置时区的时间；没有文字时使用 `[非文本消息]`。正文超过 3500 字符时会截断并附上标记。
- 使用异步 HTTP 客户端和可配置超时，HTTP 成功且 Bark JSON 响应中 `code=200` 才记为服务接收成功。该状态不代表设备已经展示通知。
- 推送失败记录错误类型后继续监听；不自动重试或保存离线队列，避免超时后重复推送。程序关闭期间及未运行期间的消息不保证补发。
- 日志不输出消息正文、Telegram 密码或带 Bark 密钥的 URL。原脚本中的密钥没有复制进仓库；如果原文件被共享，请检查相关凭据和授权。

原单文件启动方式仍然可用：`uv run python app.py`。如果已有授权 session，将 `TELEGRAM_SESSION` 指向原 session 文件的路径即可；文件名可包含 `.session` 后缀。默认相对路径基于启动时的工作目录，父目录会自动创建。

## Docker

使用 Compose 启动管理面板和持久化数据卷：

```bash
docker compose up --build -d
open http://127.0.0.1:8787
```

容器默认以非 root 用户运行，并将 `/data` 作为持久卷。首次打开页面后，在“运行配置”中保存 Telegram 和 Bark 配置，再完成 Telegram 授权并启动监听。Docker 版不会把根目录 `.env` 的 Telegram/Bark 凭据注入容器；令牌、日志选项和数据路径按 [部署指南](docs/DEPLOYMENT.md) 的白名单规则处理。

## 项目结构

| 文件 | 职责 |
| --- | --- |
| `app.py` | 兼容原有启动方式 |
| `bark_monitor/app.py` | CLI、连接生命周期、资源关闭 |
| `bark_monitor/config.py` | 配置解析及启动前校验 |
| `bark_monitor/auth.py` | 授权 session 复用、验证码和二步验证 |
| `bark_monitor/formatting.py` | 消息分类、发送者兜底、时间及通知格式 |
| `bark_monitor/notifier.py` | 异步 Bark HTTP 请求与响应校验 |
| `bark_monitor/listener.py` | Telegram 事件注册与私聊转发规则 |
| `bark_monitor/logging_setup.py` | JSON 日志、文件轮转和敏感信息脱敏 |
| `tests/` | 无外部账号依赖的回归测试 |

## 验证

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

测试覆盖配置校验、聊天类型分类、发送者边界情况、通知内容格式、登录与退出流程、Bark HTTP 成功/失败处理以及日志脱敏，不需要连接真实 Telegram。`uv.lock` 固定依赖，`.python-version` 默认选择 Python 3.13；项目要求 Python 3.11+。

Bark 的 POST JSON 格式参考 [Bark 官方文档](https://github.com/Finb/Bark/blob/master/docs/en-us/tutorial.md)，事件与登录接口参考 [Telethon 1.x 官方文档](https://docs.telethon.dev/en/stable/modules/client.html)。

## 许可证

本项目采用 MIT 许可证，详见 [LICENSE](LICENSE)。前端 Tabler Core 的许可与归属信息见 [frontend/NOTICE](frontend/NOTICE)。
