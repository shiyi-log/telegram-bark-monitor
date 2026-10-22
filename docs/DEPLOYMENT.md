# 部署指南

## 本机 Docker Compose

安装 Docker Engine 和 Docker Compose v2 后，在项目目录运行：

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f bark-monitor
```

Compose 不会把根目录 `.env` 中的 Telegram/Bark 凭据注入容器。首次启动后在管理页面填写这些凭据并保存，应用会将配置写入数据卷的 `/data/.env`。根目录 `.env` 仅用于覆盖显式映射的管理令牌与日志选项；这避免进程环境长期覆盖面板保存的配置。

Dashboard 默认监听容器内的 `0.0.0.0:8787`，Compose 将它映射到本机 `127.0.0.1:8787`。在运行 Docker 的机器上访问 `http://127.0.0.1:8787`。健康检查地址为 `/healthz`，它只确认 Web 服务可用，不代表 Telegram 已授权或 Bark 推送成功。

首次启动时，如果没有设置 `BARK_DASHBOARD_TOKEN`，应用会在数据目录生成 dashboard 访问令牌。使用随机、足够长的令牌并通过受保护的环境变量提供，可避免令牌以文件形式保存在数据卷内：

```bash
export BARK_DASHBOARD_TOKEN="$(openssl rand -hex 32)"
docker compose up --build -d
```

如果使用自动生成的令牌，可通过 `docker compose exec bark-monitor cat /data/dashboard.token` 查看。请将其作为密码保管。不要把令牌写入仓库、工单或公开日志。

`bark-data` 命名卷保存 dashboard 配置、管理令牌、日志和 Telegram session。应用会在 `/data/.env` 以 `0600` 权限持久化 Telegram 与 Bark 配置，在 `/data/dashboard.token` 以 `0600` 权限保存自动生成的管理令牌，并在 `/data/telegram_session.session` 保存 Telethon 授权。默认日志文件为 `/data/logs/bark-monitor.log`，日志目录权限为 `0700`、文件权限为 `0600`。升级容器时保留此卷。

首次启动后打开管理页面，在配置区填写 Telegram API ID、API Hash、手机号和 Bark 推送地址并保存，然后使用页面的授权流程登录 Telegram，最后启动监听。Compose 不会把项目根目录的 `.env` 整体传入容器：配置页面保存的 `/data/.env` 是 Telegram/Bark 设置的持久来源；Docker 环境中的 `TELEGRAM_SESSION` 固定为 `/data/telegram_session`，授权 session 因此保存在同一命名卷。根目录 `.env` 仅用于给下面明确列出的管理令牌和日志启动变量提供 Compose 插值值。已有 Telegram/Bark 环境配置不会自动导入容器，请在页面录入一次。

备份前先停止服务，并将卷内容放入访问受控的加密备份：

```bash
docker compose stop
docker run --rm -v bark_bark-data:/data:ro -v "$PWD/backups:/backup" alpine \
  tar -czf /backup/bark-data-$(date +%F).tar.gz -C /data .
docker compose start
```

首次从 CLI 迁移配置时，可先启动一次 Compose 创建命名卷，再通过标准输入把受保护的本地配置写入卷；命令不会把配置内容回显到终端：

```bash
docker compose up -d
docker compose exec -T bark-monitor sh -c 'umask 077; cat > /data/.env' < .env
docker compose restart
```

### 日志配置

Compose 会把日志写入数据卷，默认保留主日志和 5 个轮转备份。可在宿主机环境或项目根目录 `.env` 设置以下 Compose 启动变量；Compose 只传递这些明确列出的日志项和 `BARK_DASHBOARD_TOKEN`，不会传入其他 `.env` 项：

```dotenv
BARK_LOG_LEVEL=INFO
BARK_LOG_MAX_BYTES=5242880
BARK_LOG_BACKUP_COUNT=5
BARK_LOG_TO_FILE=true
# 可选：指定完整文件路径；设置后优先于 BARK_LOG_DIR
# 容器内路径必须位于 /data 下，默认值为空
# BARK_LOG_FILE=/data/logs/custom.log
# BARK_LOG_DIR=/data/logs
BARK_DASHBOARD_TOKEN=
```

根目录 `.env` 是 Compose 插值输入，不会作为文件挂载到容器内；其中的 Telegram/Bark 配置项不会被读取。控制台和文件使用单行 JSON 格式。日志会自动隐藏 URL、Bearer 令牌、手机号、配置密钥和 Telethon 异常详情，不记录 Telegram 消息正文。Uvicorn 运行和错误日志并入统一处理器，逐请求访问日志关闭。新建日志目录使用 `0700` 权限，既有自定义目录保留原权限。`BARK_LOG_TO_FILE=false` 时只保留容器标准输出，可用 `docker compose logs` 查看。

Compose 项目名前缀决定卷名；上例假设目录名为 `bark`，可用 `docker volume ls` 确认实际名称。执行 `docker compose down` 会保留数据。不要使用 `docker compose down -v`，它会删除持久卷。

## 公网部署

默认端口只绑定本机回环地址。需要公网访问时，在宿主机反向代理中配置 TLS、访问控制和请求限制，再代理到 `127.0.0.1:8787`。不要直接将无 TLS 的 dashboard 端口暴露到公网。设置并轮换 `BARK_DASHBOARD_TOKEN`，限制宿主机与备份中数据卷的访问。

## 发布流水线

`.github/workflows/ci.yml` 在推送到 `main`、Pull Request 和手动触发时运行 Python 3.11/3.13 测试、Ruff、sdist/wheel 构建与包内容校验、前端 `npm ci`/生产依赖审计/构建，以及 Docker 冒烟检查。容器检查覆盖 `/healthz`、自动令牌与 API 鉴权、首页及 JS/CSS、UI 配置保存和重启持久化；标签发布也会先运行同一检查。CI 只使用模拟数据，不连接真实 Telegram 或 Bark 服务。

创建与 `pyproject.toml` 版本一致的 `vX.Y.Z` 标签并推送后，`.github/workflows/release.yml` 会构建 wheel 和源码包、生成 SHA-256 校验文件、创建 GitHub Release，并将 `linux/amd64` 与 `linux/arm64` 镜像推送到 GHCR。镜像名为 `ghcr.io/<owner>/<repository>:<version>`（例如标签 `v0.1.0` 对应镜像 `:0.1.0`），同时更新 `latest` 标签。

GitHub Actions 需要允许工作流创建 Release 和写入 Packages；工作流按 job 限定 `contents` 与 `packages` 权限，不需要 PyPI 密钥。发布后首次拉取私有仓库镜像时，需用有权访问该 package 的 GitHub 凭据登录 GHCR。

GitHub Release 提供 wheel、sdist 和 `SHA256SUMS`。将三个文件下载到同一目录后，可在 Linux 上执行 `sha256sum -c SHA256SUMS` 校验。sdist 只包含源码、锁文件、文档和构建后的页面资源，不包含 `node_modules`、虚拟环境、运行数据或 `.env`。

```bash
docker pull ghcr.io/<owner>/<repository>:<version>
```
