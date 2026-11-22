"""The Connect websocket loop: refused handshakes, odd notices and reconnect delays."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Self
from unittest.mock import Mock, patch

import aiohttp
import pytest

from custom_components.savefamily.connect.push import AUTH_RETRY_DELAY, SaveFamilyConnectPush

DID = "860000000000001"


class FakeWebSocket:
    """One connection: the messages the server sends, then the close."""

    close_code = 1000

    def __init__(self, notices: list[Any]) -> None:
        self._messages = [
            Mock(type=aiohttp.WSMsgType.TEXT, data=notice if isinstance(notice, str) else json.dumps(notice))
            for notice in notices
        ]

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Any:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class FakeClient:
    """Answers each connection attempt from a script: a list of notices, or an exception to raise."""

    def __init__(self, attempts: list[Any]) -> None:
        self._attempts = attempts
        self.refreshes = 0
        self._token = 0
        self._forced_token: int | None = None
        self._ws_token: int | None = None

    async def async_ws_connect(self) -> FakeWebSocket:
        if not self._attempts:
            raise asyncio.CancelledError
        self._ws_token = self._token
        attempt = self._attempts.pop(0)
        if isinstance(attempt, BaseException):
            raise attempt
        return FakeWebSocket(attempt)

    async def async_refresh_for_websocket(self) -> bool:
        # Same rule as the real client: one refresh per refused access token.
        if self._ws_token == self._forced_token:
            return False
        self.refreshes += 1
        self._token += 1
        self._forced_token = self._token
        return True


def refused(status: int = 401) -> aiohttp.WSServerHandshakeError:
    return aiohttp.WSServerHandshakeError(Mock(), (), status=status)


async def _run(client: FakeClient) -> tuple[list[str | None], list[float]]:
    """Run the loop until the script ends; return the updates and the delays it waited."""
    updates: list[str | None] = []
    delays: list[float] = []

    async def no_wait(delay: float) -> None:
        delays.append(delay)

    with (
        patch("custom_components.savefamily.connect.push.asyncio.sleep", no_wait),
        pytest.raises(asyncio.CancelledError),
    ):
        await SaveFamilyConnectPush(client, updates.append).async_run()
    return updates, delays


async def test_refused_fresh_token_waits_instead_of_rotating_the_session() -> None:
    client = FakeClient([refused(), refused(), refused(403)])

    _, delays = await _run(client)

    # The first refusal refreshes once; the fresh token is refused too, so no more refreshes.
    assert client.refreshes == 1
    assert delays[1:] == [AUTH_RETRY_DELAY, AUTH_RETRY_DELAY]


async def test_odd_notices_and_errors_do_not_stop_live_updates() -> None:
    client = FakeClient(
        [
            [{"type": ["sos"]}, {"type": {"x": 1}}, "not json", [1, 2], {"type": "heart-rate-processed"}],
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
            [{"type": "position-processed", "deviceIdentificator": DID}, {"type": "sos"}],
        ]
    )

    updates, _ = await _run(client)

    assert updates == [DID, None]


async def test_server_closing_right_after_the_greeting_backs_off() -> None:
    client = FakeClient([[{"type": "connected"}]] * 4)

    with patch("custom_components.savefamily.connect.push.time.monotonic", side_effect=[0, 1] * 4):
        _, delays = await _run(client)

    assert delays == [2, 4, 8, 16]


async def test_connection_that_stayed_open_resets_the_backoff() -> None:
    client = FakeClient([refused(500), refused(500), [{"type": "connected"}], refused(500)])

    with patch("custom_components.savefamily.connect.push.time.monotonic", side_effect=[0, 120]):
        _, delays = await _run(client)

    assert delays == [2, 4, 2, 4]
