# Bark Monitor 前端

这是 Telegram → Bark 监听器的本地管理页面，使用 [Vite](https://vite.dev/) 和 [Tabler](https://github.com/tabler/tabler) 构建。页面采用原生 JavaScript，避免为一个本地控制台引入额外运行时框架。

## 开发

需要 Node.js 22.12 或更新版本：

```bash
npm ci
npm run dev
```

开发服务器默认运行在 `http://localhost:5173`。开发时页面会请求同源的 `/api/*` 接口；可以通过 Vite 反向代理把接口转发到运行中的 Python 服务。

## 构建

```bash
npm run build
```

构建结果输出到 `../bark_monitor/static/`，供 Python 后端打包或通过 FastAPI `StaticFiles` 直接托管。不要把 `.env`、Telegram session、Bark device key 或 API hash 放进前端源码；敏感字段始终使用密码输入框，并且不会从配置接口回填。

页面目前调用这些后端接口：

```text
GET  /api/status
GET  /api/config
PUT  /api/config
POST /api/monitor/start
POST /api/monitor/stop
POST /api/bark/test
POST /api/telegram/request-code
POST /api/telegram/sign-in
POST /api/telegram/password
GET  /api/logs?limit=100
```

`/api/status` 返回 `status`、`connected`、`authorized`、`listening`、`auth_state`、`stats`、`last_event_at` 和 `last_error` 等字段；状态和授权状态可直接显示中文。`/api/logs` 返回 `{ "items": [{ "timestamp", "level", "logger", "message" }] }`，页面只使用文本节点渲染日志，避免日志内容被当作 HTML 执行。

管理令牌只保存在当前页面内存中，使用 `Authorization: Bearer <token>` 发送，不写入 localStorage 或 cookie。后端若不需要令牌，令牌输入框可以留空。

## 依赖与许可证

`@tabler/core` 固定为 `1.6.1`，Tabler 使用 MIT 许可证。这里只使用核心 CSS/JavaScript，没有复制 Tabler 示例页的第三方插件集合（例如图表插件）；如后续加入第三方组件，请在发布产物中补充其许可证声明。

第三方许可证说明见 [`NOTICE`](./NOTICE)。
