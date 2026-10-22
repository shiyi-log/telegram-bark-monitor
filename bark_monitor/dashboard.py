"""本地管理面板：提供配置、授权、启停、健康检查和脱敏日志接口。"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
from collections import deque
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .app import configure_logging
from .logging_setup import SafeRedactionFilter, redact_text
from .service import MonitorService, ServiceError

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).with_name("static")


class MemoryLogHandler(logging.Handler):
    """保存最近日志，供页面轮询；进程重启后日志自然清空。"""

    def __init__(self, limit: int = 200):
        super().__init__()
        self.items: deque[dict[str, str]] = deque(maxlen=limit)
        # 页面日志与控制台、文件日志使用同一套脱敏规则。
        self.addFilter(SafeRedactionFilter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            self.items.append(
                {
                    "timestamp": self.formatter.formatTime(record, "%Y-%m-%d %H:%M:%S"),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": _redact_log(message),
                }
            )
        except Exception:
            self.handleError(record)


def _redact_log(message: str) -> str:
    """将管理面板日志交给公共脱敏实现处理。"""

    return redact_text(message)


def _token_from_header(authorization: str | None, x_bark_token: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return (x_bark_token or "").strip()


def _dashboard_token_file() -> Path:
    """返回数据卷中的管理令牌文件路径。"""

    return Path(os.environ.get("BARK_DATA_DIR", "data")).expanduser() / "dashboard.token"


def _read_dashboard_token() -> str:
    """优先读取环境变量；未配置时再读取 0600 权限的持久令牌文件。"""

    token = os.environ.get("BARK_DASHBOARD_TOKEN", "").strip()
    if token:
        return token
    try:
        return _dashboard_token_file().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _ensure_dashboard_token() -> str:
    """为非本机监听生成一次性令牌，并以私有权限保存在数据目录。"""

    import secrets

    token = _read_dashboard_token()
    if token:
        return token
    token_path = _dashboard_token_file()
    token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_urlsafe(32)
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
        handle.write("\n")
    logger.info("已生成管理令牌: path=%s", token_path)
    return token


def create_app(service: MonitorService | None = None) -> FastAPI:
    """创建可测试的 FastAPI 应用，不在导入时连接 Telegram。"""

    monitor = service or MonitorService()
    log_handler = MemoryLogHandler()
    log_handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(log_handler)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info("管理面板已启动")
        try:
            yield
        finally:
            await monitor.close()
            root_logger.removeHandler(log_handler)
            logger.info("管理面板已关闭")

    app = FastAPI(title="Telegram Bark Monitor", version="0.1.0", lifespan=lifespan)
    app.state.monitor = monitor
    app.state.log_handler = log_handler

    async def require_token(
        request: Request,
        authorization: str | None = Header(default=None),
        x_bark_token: str | None = Header(default=None),
    ) -> None:
        expected = _read_dashboard_token()
        if not expected:
            return
        if _token_from_header(authorization, x_bark_token) != expected:
            logger.warning("拒绝未授权管理请求: path=%s", request.url.path)
            raise HTTPException(status_code=401, detail="需要管理令牌")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status", dependencies=[Depends(require_token)])
    async def status() -> dict[str, Any]:
        return monitor.status()

    @app.get("/api/config", dependencies=[Depends(require_token)])
    async def config() -> dict[str, Any]:
        return monitor.config_store.public()

    @app.put("/api/config", dependencies=[Depends(require_token)])
    async def update_config(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return monitor.config_store.update(payload)
        except ServiceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/logs", dependencies=[Depends(require_token)])
    async def logs(limit: int = 100) -> dict[str, Any]:
        limit = max(1, min(limit, 200))
        return {"items": list(log_handler.items)[-limit:]}

    @app.post("/api/monitor/start", dependencies=[Depends(require_token)])
    async def start_monitor() -> dict[str, Any]:
        try:
            return await monitor.start()
        except (ServiceError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/monitor/stop", dependencies=[Depends(require_token)])
    async def stop_monitor() -> dict[str, Any]:
        return await monitor.stop()

    @app.post("/api/bark/test", dependencies=[Depends(require_token)])
    async def test_bark() -> dict[str, Any]:
        try:
            accepted = await monitor.test_bark()
            return {"accepted": accepted}
        except (ServiceError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/telegram/request-code", dependencies=[Depends(require_token)])
    async def request_code(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            phone = (payload or {}).get("phone")
            return await monitor.request_code(phone)
        except (ServiceError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/telegram/sign-in", dependencies=[Depends(require_token)])
    async def sign_in(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await monitor.sign_in_code(str(payload.get("code", "")))
        except (ServiceError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/telegram/password", dependencies=[Depends(require_token)])
    async def password(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await monitor.sign_in_password(str(payload.get("password", "")))
        except (ServiceError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if STATIC_DIR.is_dir() and (STATIC_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def frontend(path: str) -> Any:
            requested = STATIC_DIR / path
            if path and requested.is_file() and STATIC_DIR in requested.parents:
                return FileResponse(requested)
            index = STATIC_DIR / "index.html"
            if index.is_file():
                return FileResponse(index)
            return JSONResponse({"detail": "前端尚未构建"}, status_code=404)

    return app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动 Telegram Bark Monitor 管理面板")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认只监听本机")
    parser.add_argument("--port", type=int, default=8787, help="监听端口")
    parser.add_argument("--env-file", type=Path, default=None, help="配置文件路径")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config_path = args.env_file.expanduser() if args.env_file is not None else None
    if config_path is not None:
        if not os.environ.get("BARK_CONFIG_FILE"):
            os.environ["BARK_CONFIG_FILE"] = str(config_path)
    else:
        configured_path = os.environ.get("BARK_CONFIG_FILE", "").strip()
        if configured_path:
            config_path = Path(configured_path).expanduser()
        else:
            data_dir = Path(os.environ.get("BARK_DATA_DIR", ".")).expanduser()
            config_path = data_dir / ".env" if data_dir != Path(".") else Path.cwd() / ".env"
    # 运行配置由 ConfigStore 动态读取，仅将日志和访问控制选项加载到进程环境。
    for key, value in dotenv_values(config_path, interpolate=False).items():
        if value is not None and (key.startswith("BARK_LOG_") or key == "BARK_DASHBOARD_TOKEN"):
            os.environ.setdefault(key, value)
    configure_logging()
    try:
        bind_ip = ipaddress.ip_address(args.host)
    except ValueError:
        bind_ip = None
    if (bind_ip is None or not bind_ip.is_loopback) and not os.environ.get(
        "BARK_DASHBOARD_TOKEN", ""
    ).strip():
        _ensure_dashboard_token()
    import uvicorn

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
