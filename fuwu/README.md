# Fuwu 集成

`project.json` 是登记到 Fuwu 的公开项目元数据。它不包含 Fuwu 地址、账号、令牌或 GitHub 凭据。

## 本地登记与发布

先准备受保护的环境变量。生产环境优先使用短期或专用的 `FUWU_TOKEN`；不要把令牌写进仓库、命令历史或日志。

```bash
export FUWU_BASE_URL="https://<fuwu-host>"
export FUWU_TOKEN="..."

python scripts/fuwu_release.py register \
  --project bark \
  --project-file fuwu/project.json

python scripts/fuwu_release.py publish \
  --project bark \
  --channel stable \
  --manifest dist/manifest.json
```

`publish` 会先确认项目仓库绑定，再检查同版本发行记录；随后请求 Fuwu 从 GitHub Release 远端 staging，最后发布并重新读取发行记录核验资产。遇到同版本不同 revision 或 staging 冲突时会失败，不会覆盖现有不可变对象。

## GitHub Actions

`release.yml` 完成 GitHub Release 和 GHCR 双架构镜像后，会调用同一脚本。只有明确设置 `FUWU_ENABLED=true` 才启用发布；启用后服务地址、认证和匿名下载验收地址均为必需配置，缺失时工作流失败，不会把一次 GitHub Release 误报为 Fuwu 已发布。`false` 或未设置时，即使残留服务地址和凭据也不会发布，工作流会在摘要中标记为禁用。手动补发要求先明确启用，否则直接失败。

仓库中使用以下安全配置：

- Repository variable `FUWU_BASE_URL`：只填 HTTPS 服务根地址，例如 `https://fuwu.example`；本地联调才允许 loopback HTTP。
- Repository variable `FUWU_ENABLED`：设置为 `true` 才启用 Fuwu 发布；正式启用时必须同时配置以下变量和 secrets。
- Repository secret `FUWU_TOKEN`：Fuwu 管理令牌。
- 如必须使用账号密码，可改用 `FUWU_USERNAME` 和 `FUWU_PASSWORD` 两个 secrets，CI 不记录其值。
- Repository variable `FUWU_PUBLIC_BASE_URL`：匿名下载服务的 HTTPS 根地址；启用 Fuwu 后会强制读取 latest、manifest、`SHA256SUMS` 并逐个核验所有资产。
- 可选 Repository variable `FUWU_PUBLIC_MODE`：设为 `api` 使用 Fuwu 匿名下载 API，或设为 `object` 直接验证对象存储；默认 `object`。API 模式目前只支持 stable，beta 会在远端发布前被拒绝。对象模式可通过 `FUWU_PUBLIC_PREFIX` 指定默认 `releases` 之外的存储前缀，并支持 beta。

首次登记使用手动 `workflow_dispatch` 的 `register_project=true`，需要有全项目范围和 `projects.manage` 权限的管理凭据；该选项只用于首次登记，常规标签发布不会尝试使用发布账号变更项目目录。之后配置仅具备 Bark 项目范围的发布/读取权限的凭据。由于 Fuwu 当前没有长期项目级 token 创建接口，可使用 Bark 范围的 `release-manager` 账号，由工作流以 `FUWU_USERNAME`/`FUWU_PASSWORD` 登录获取短期 token。公共客户端只读取 Fuwu 发布后的 HTTPS 地址，不携带管理凭据。

公开下载入口为 `https://fuwu.1213.cc/api/downloads/bark/stable/latest.json`；指定版本的 manifest、`SHA256SUMS` 和资产使用 `https://fuwu.1213.cc/api/downloads/bark/{version}/{filename}`。项目登记信息也提供这两个下载模板，供安装器直接从 Fuwu 获取，不依赖 GitHub Release 下载。
