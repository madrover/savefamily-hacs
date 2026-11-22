"""Live updates from the SaveFamily Connect websocket.

The server pushes small JSON notices such as `{"type": "position-processed", "deviceIdentificator": ...}`
when it has stored new data for a watch, and alert notices such as `{"type": "sos", ...}`. A notice about
something the integration exposes triggers an early poll; the regular poll stays as the fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable

import aiohttp

from ..core.protocol import SaveFamilyAuthError, SaveFamilyError
from .client import ALERT_TYPES, SaveFamilyConnectClient

_LOGGER = logging.getLogger(__name__)

# Reconnect delay doubles from 2 s up to this cap, like the portal.
MAX_RECONNECT_DELAY = 60
# When the session needs a new login the coordinator has already raised the reauth flow, and a
# successful reauth reloads the entry (and this task); until then, retry rarely.
AUTH_RETRY_DELAY = 900
# A connection open this long (seconds) resets the reconnect delay; a server that greets and closes
# at once does not get a new connection every 2 seconds.
STABLE_CONNECTION = 60
# Notices worth an early poll. Heart rate, oxygen and photos are not exposed yet.
DATA_NOTICES = frozenset({"position-processed", "steps-processed"})


class SaveFamilyConnectPush:
    def __init__(self, client: SaveFamilyConnectClient, on_update: Callable[[str | None], None]) -> None:
        self._client = client
        self._on_update = on_update

    async def async_run(self) -> None:
        """Keep the websocket open until cancelled, reconnecting with backoff."""
        attempt = 0
        while True:
            delay = None
            try:
                if await self._async_listen():
                    attempt = 0
            except aiohttp.WSServerHandshakeError as exc:
                _LOGGER.debug("Connect websocket refused: HTTP %s", exc.status)
                if exc.status in (401, 403):
                    try:
                        if not await self._client.async_refresh_for_websocket():
                            # A fresh token was refused too while the REST API accepts it.
                            delay = AUTH_RETRY_DELAY
                    except SaveFamilyAuthError:
                        delay = AUTH_RETRY_DELAY
                    except SaveFamilyError as err:
                        _LOGGER.debug("Token refresh for the websocket failed: %s", err)
            except SaveFamilyAuthError as exc:
                _LOGGER.debug("Connect websocket needs a new login: %s", exc)
                delay = AUTH_RETRY_DELAY
            except (aiohttp.ClientError, TimeoutError, SaveFamilyError) as exc:
                _LOGGER.debug("Connect websocket error: %s", exc)
            except Exception:
                # Polling still works; keep live updates going rather than ending the task.
                _LOGGER.exception("Unexpected error in the Connect websocket")
            attempt += 1
            await asyncio.sleep(delay or min(2**attempt, MAX_RECONNECT_DELAY))

    async def _async_listen(self) -> bool:
        """Read one connection until it closes; return whether it was greeted and stayed open."""
        greeted = False
        async with await self._client.async_ws_connect() as ws:
            opened = time.monotonic()
            _LOGGER.debug("Connect websocket connected")
            async for message in ws:
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    notice = json.loads(message.data)
                except ValueError:
                    continue
                if not isinstance(notice, dict):
                    continue
                notice_type = notice.get("type")
                if not isinstance(notice_type, str):
                    continue
                # Field names only: values can include the child's name or position.
                _LOGGER.debug("Connect websocket notice: %s %s", notice_type, sorted(map(str, notice)))
                if notice_type == "connected":
                    greeted = True
                    continue
                if notice_type not in DATA_NOTICES and notice_type not in ALERT_TYPES:
                    continue
                # Data notices name the watch; alert notices (e.g. `sos`) do not.
                did = notice.get("deviceIdentificator")
                self._on_update(did if isinstance(did, str) and did else None)
            _LOGGER.debug("Connect websocket closed: %s", ws.close_code)
        return greeted and time.monotonic() - opened >= STABLE_CONNECTION
