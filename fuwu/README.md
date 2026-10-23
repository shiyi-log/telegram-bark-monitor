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

`release.yml` 完成 GitHub Release 和 GHCR 双架构镜像后，会在 `FUWU_BASE_URL` 已配置且存在凭据时调用同一脚本。缺少配置时该 job 会明确跳过，不会把一次 GitHub Release 误报为 Fuwu 已发布。

仓库中使用以下安全配置：

- Repository variable `FUWU_BASE_URL`：只填 HTTPS 服务根地址，例如 `https://fuwu.example`；本地联调才允许 loopback HTTP。
- Repository secret `FUWU_TOKEN`：Fuwu 管理令牌。
- 如必须使用账号密码，可改用 `FUWU_USERNAME` 和 `FUWU_PASSWORD` 两个 secrets，CI 不记录其值。

如果 Fuwu 使用项目范围权限，令牌至少需要项目读取、项目管理（首次登记）和 release storage publish/read 权限。公共客户端只读取 Fuwu 发布后的 HTTPS 对象，不携带管理令牌。
