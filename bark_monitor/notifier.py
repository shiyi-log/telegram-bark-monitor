"""Bark 通知客户端。"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _suppress_sensitive_http_logs() -> None:
    """禁止 HTTP 客户端在 INFO 日志中打印包含 Bark 密钥的 URL。"""

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class BarkNotifier:
    """使用可复用异步 HTTP 客户端发送 Bark JSON 通知。"""

    def __init__(
        self,
        push_url: str,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ):
        self.push_url = push_url
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> BarkNotifier:
        _suppress_sensitive_http_logs()
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(self, title: str, body: str) -> bool:
        """返回 Bark 是否确认接收通知。"""

        if self._client is None:
            raise RuntimeError("BarkNotifier 必须在 async with 中使用")
        try:
            response = await self._client.post(
                self.push_url,
                json={"title": title, "body": body},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("code") != 200:
                logger.warning("Bark 未确认接收通知，请检查设备密钥和服务配置")
                return False
            logger.info("Bark 服务已接收通知")
            return True
        except httpx.HTTPError as exc:  # 短暂网络故障不应中断 Telegram 监听
            logger.warning("Bark 推送失败，错误类型: %s", type(exc).__name__)
            return False
        except (TypeError, ValueError) as exc:
            logger.warning("Bark 响应格式异常，错误类型: %s", type(exc).__name__)
            return False
