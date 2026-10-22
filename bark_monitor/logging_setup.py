"""应用日志配置与敏感信息脱敏工具。

日志处理器统一放在本模块，命令行、管理面板和测试可以复用同一套安全规则。
配置函数只管理自己创建的处理器，不会移除 pytest、uvicorn 或调用方注册的处理器。
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5
MAX_MAX_BYTES = 100 * 1024 * 1024
MAX_BACKUP_COUNT = 20
_HANDLER_MARKER = "_bark_monitor_handler"

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_PHONE_RE = re.compile(r"(?<!\d)\+?\d[\d\s().-]{7,}\d(?!\d)")
_SENSITIVE_FIELD_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:token|secret|password|passwd|api[_-]?hash|device[_-]?key)\b\s*"
    r"[:=]\s*[\"']?)(?P<value>[^\s,;}\"']+)"
)
_BODY_FIELD_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:body|payload|content)\b\s*[:=]\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)
_SECRET_ENV_RE = re.compile(
    r"(?i)(?:password|passwd|secret|token|api[_-]?hash|device[_-]?key|access[_-]?key|"
    r"private[_-]?key|authorization|phone|session)"
)


def _environment_secrets(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """收集环境中的敏感值，按长度倒序避免短值遮挡长值。"""

    values = os.environ if environ is None else environ
    secrets = {
        str(value)
        for key, value in values.items()
        if _SECRET_ENV_RE.search(str(key)) and value and len(str(value)) >= 4
    }
    return tuple(sorted(secrets, key=len, reverse=True))


def redact_text(message: Any, secrets: tuple[str, ...] | None = None) -> str:
    """删除日志中的 URL、令牌、手机号和常见敏感字段值。"""

    text = str(message)
    # 先隐藏 URL，避免 Bark 设备密钥出现在路径或查询参数中。
    text = _URL_RE.sub("[已隐藏URL]", text)
    text = _BEARER_RE.sub("Bearer [已隐藏令牌]", text)
    text = _SENSITIVE_FIELD_RE.sub(r"\g<prefix>[已隐藏值]", text)
    text = _BODY_FIELD_RE.sub(r"\g<prefix>[已隐藏正文]", text)
    text = _PHONE_RE.sub("[已隐藏手机号]", text)
    for secret in secrets if secrets is not None else _environment_secrets():
        if secret:
            text = text.replace(secret, "[已隐藏敏感值]")
    return text


class SafeRedactionFilter(logging.Filter):
    """在处理器级别统一脱敏，并隐藏 Telethon 异常详情。"""

    def __init__(
        self,
        extra_secrets: tuple[str, ...] = (),
        environ: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__()
        self._extra_secrets = tuple(value for value in extra_secrets if value)
        self._environ = environ

    def filter(self, record: logging.LogRecord) -> bool:
        """原地替换消息，确保后续处理器也只能看到脱敏内容。"""

        try:
            if record.name.startswith("telethon") and record.exc_info:
                # Telethon 的异常文本可能包含手机号、请求参数或会话信息。
                message = "Telethon 日志异常详情已隐藏"
            else:
                message = record.getMessage()
        except Exception:
            message = "日志消息格式化失败"
        secrets = tuple(
            sorted(
                set(_environment_secrets(self._environ)).union(self._extra_secrets),
                key=len,
                reverse=True,
            )
        )
        record.msg = redact_text(message, secrets)
        record.args = ()
        if record.exc_info:
            if record.name.startswith("telethon"):
                record.exc_text = "Telethon 异常详情已隐藏"
            else:
                exception = logging.Formatter().formatException(record.exc_info)
                record.exc_text = redact_text(exception, secrets)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_text(record.exc_text, secrets)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info, secrets)
        return True


class JsonFormatter(logging.Formatter):
    """将日志编码为便于采集的单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        """输出时间、级别、记录器和脱敏后的消息。"""

        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_text:
            payload["exception"] = record.exc_text
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _parse_level(value: str | None) -> int:
    """解析日志级别；未知值回退到 INFO，避免启动日志配置阻断应用。"""

    if not value:
        return logging.INFO
    normalized = value.strip().upper()
    level = logging.getLevelNamesMapping().get(normalized)
    return level if isinstance(level, int) else logging.INFO


def _bounded_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    """读取有上限的整数配置，非法值使用安全默认值。"""

    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _log_file_path(environ: Mapping[str, str]) -> Path | None:
    """根据环境变量确定日志文件；显式关闭值可以禁用文件日志。"""

    if environ.get("BARK_LOG_TO_FILE", "").strip().lower() in {"0", "false", "no", "off"}:
        return None
    configured_file = environ.get("BARK_LOG_FILE", "").strip()
    if configured_file:
        return Path(configured_file).expanduser()
    configured_dir = environ.get("BARK_LOG_DIR", "").strip()
    if configured_dir:
        return Path(configured_dir).expanduser() / "bark-monitor.log"
    data_dir = environ.get("BARK_DATA_DIR", "").strip()
    base_dir = Path(data_dir).expanduser() if data_dir else Path("data")
    return base_dir / "logs" / "bark-monitor.log"


def _secure_parent(path: Path) -> None:
    """创建缺失的日志目录并限制新目录仅当前用户可访问。

    不修改已经存在的父目录权限，避免用户把日志放到共享目录或系统目录时
    产生意外的全局权限副作用。
    """

    directory = path.parent
    missing: list[Path] = []
    while not directory.exists():
        missing.append(directory)
        if directory.parent == directory:
            break
        directory = directory.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            continue
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass


def _secure_file(path: Path) -> None:
    """将日志文件权限收紧到当前用户可读写。"""

    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _remove_owned_handlers(root: logging.Logger) -> None:
    """删除本模块上一次配置的处理器，保持重复调用幂等。"""

    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            root.removeHandler(handler)
            handler.close()
    for handler in root.handlers:
        for log_filter in list(handler.filters):
            if getattr(log_filter, "_bark_monitor_filter", False):
                handler.removeFilter(log_filter)


def configure_logging(
    *,
    environ: Mapping[str, str] | None = None,
    level: str | None = None,
    log_file: str | Path | None = None,
) -> None:
    """配置控制台和滚动文件日志。

    ``BARK_LOG_LEVEL``、``BARK_LOG_FILE``、``BARK_LOG_DIR``、``BARK_LOG_MAX_BYTES``、
    ``BARK_LOG_BACKUP_COUNT`` 和 ``BARK_LOG_TO_FILE`` 均支持环境变量覆盖。重复调用
    会重建本模块创建的处理器，但保留调用方已有的处理器。
    """

    env = os.environ if environ is None else environ
    selected_level = _parse_level(level or env.get("BARK_LOG_LEVEL"))
    max_bytes = _bounded_int(env.get("BARK_LOG_MAX_BYTES"), DEFAULT_MAX_BYTES, 1, MAX_MAX_BYTES)
    backup_count = _bounded_int(
        env.get("BARK_LOG_BACKUP_COUNT"), DEFAULT_BACKUP_COUNT, 0, MAX_BACKUP_COUNT
    )
    path = Path(log_file).expanduser() if log_file is not None else _log_file_path(env)

    root = logging.getLogger()
    _remove_owned_handlers(root)
    root.setLevel(selected_level)

    formatter = JsonFormatter()
    # 先保护调用方已经注册的根处理器（例如测试捕获器或服务管理面板）。
    for handler in root.handlers:
        log_filter = SafeRedactionFilter(environ=env)
        log_filter._bark_monitor_filter = True
        handler.addFilter(log_filter)

    console = logging.StreamHandler()
    console.setLevel(selected_level)
    console.setFormatter(formatter)
    console.addFilter(SafeRedactionFilter(environ=env))
    console._bark_monitor_handler = True
    root.addHandler(console)

    if path is not None:
        _secure_parent(path)
        path.touch(exist_ok=True)
        _secure_file(path)
        file_handler = SecureRotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=False,
        )
        _secure_file(path)
        file_handler.setLevel(selected_level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(SafeRedactionFilter(environ=env))
        file_handler._bark_monitor_handler = True
        root.addHandler(file_handler)

    # 第三方库的 INFO 日志可能包含请求 URL；Telethon 异常详情也不进入应用日志。
    for logger_name in ("httpx", "httpcore", "telethon"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


__all__ = [
    "JsonFormatter",
    "SafeRedactionFilter",
    "configure_logging",
    "redact_text",
]


class SecureRotatingFileHandler(RotatingFileHandler):
    """轮转后仍为新日志文件设置仅当前用户可读写的权限。"""

    def _open(self):
        stream = super()._open()
        _secure_file(Path(self.baseFilename))
        return stream
