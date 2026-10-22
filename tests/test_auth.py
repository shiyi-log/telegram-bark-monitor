import pytest

from bark_monitor.auth import login_telegram


class AuthorizedClient:
    def __init__(self):
        self.connect_calls = 0
        self.send_code_calls = []

    def is_connected(self):
        return True

    async def connect(self):
        self.connect_calls += 1

    async def is_user_authorized(self):
        return True

    async def send_code_request(self, phone):
        self.send_code_calls.append(phone)


class LoginClient:
    def __init__(self, sign_in_error=None):
        self.connected = False
        self.codes = []
        self.sign_ins = []
        self.sign_in_error = sign_in_error

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return False

    async def send_code_request(self, phone):
        self.codes.append(phone)

    async def sign_in(self, *args, **kwargs):
        self.sign_ins.append((args, kwargs))
        if self.sign_in_error is not None and not kwargs:
            raise self.sign_in_error


@pytest.mark.asyncio
async def test_authorized_session_does_not_prompt_or_request_code():
    client = AuthorizedClient()

    def unexpected_prompt(_):
        pytest.fail("authorized sessions must not prompt")

    await login_telegram(client, None, input_fn=unexpected_prompt)

    assert client.connect_calls == 0
    assert client.send_code_calls == []


@pytest.mark.asyncio
async def test_login_prompts_for_phone_and_code_then_uses_2fa_password_verbatim():
    from telethon.errors import SessionPasswordNeededError

    client = LoginClient(SessionPasswordNeededError(None))
    answers = iter([" +8613800000000 ", " 12345 "])
    password_prompts = []

    def input_fn(prompt):
        return next(answers)

    def password_input_fn(prompt):
        password_prompts.append(prompt)
        return " pass word "

    await login_telegram(
        client,
        None,
        input_fn=input_fn,
        password_input_fn=password_input_fn,
    )

    assert client.connected is True
    assert client.codes == ["+8613800000000"]
    assert client.sign_ins == [
        (("+8613800000000", "12345"), {}),
        ((), {"password": " pass word "}),
    ]
    assert password_prompts


@pytest.mark.asyncio
async def test_non_2fa_authorization_error_is_propagated():
    client = LoginClient(RuntimeError("authorization failed"))

    with pytest.raises(RuntimeError, match="authorization failed"):
        await login_telegram(client, "+8613800000000", input_fn=lambda _: "12345")
