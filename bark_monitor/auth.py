"""Telegram 交互式授权流程。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from getpass import getpass
from typing import Any

from telethon.errors import SessionPasswordNeededError


async def login_telegram(
    client: Any,
    phone: str | None,
    telegram_password: str | None = None,
    input_fn: Callable[[str], str] = input,
    password_input_fn: Callable[[str], str] = getpass,
) -> None:
    """连接并授权 Telethon 客户端，仅在需要时提示用户输入信息。"""

    if not client.is_connected():
        await client.connect()
    if await client.is_user_authorized():
        return

    phone = (phone or await asyncio.to_thread(input_fn, "请输入 Telegram 手机号: ")).strip()
    if not phone:
        raise ValueError("首次登录必须提供 TELEGRAM_PHONE")
    await client.send_code_request(phone)
    code = (await asyncio.to_thread(input_fn, "请输入 Telegram 发送的验证码: ")).strip()
    if not code:
        raise ValueError("Telegram 验证码不能为空")
    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError as exc:
        password = telegram_password or await asyncio.to_thread(
            password_input_fn, "请输入 Telegram 二步验证密码: "
        )
        if not password:
            raise ValueError("Telegram 二步验证需要密码") from exc
        await client.sign_in(password=password)
