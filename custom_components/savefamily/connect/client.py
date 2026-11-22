"""Async client for the SaveFamily Connect REST gateway.

Connect is a separate platform from Classic (tgelec/myaqsh): plain JSON over HTTPS,
a JWT access token (15 minutes, sent as a cookie) and a refresh token that the server rotates
on every refresh, each time with a fresh 30-day window. Login needs an emailed code
once; after that the stored refresh token keeps the session alive while Home
Assistant keeps polling. See docs/CONNECT.md.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Callable, Container
from dataclasses import replace
from datetime import UTC, datetime
from json import loads as json_loads
from typing import Any
from urllib.parse import urlencode

import aiohttp

from ..core.protocol import (
    ALARM_FETCH_COUNT,
    ALARM_TYPE_DISCONNECT,
    ALARM_TYPE_OTHER,
    ALARM_TYPE_RECONNECT,
    COMMAND_FIND_WATCH,
    COMMAND_POWER_OFF,
    COMMAND_REQUEST_LOCATION,
    COMMAND_RESTART,
    DEVICE_OFFLINE_MESSAGE,
    DEVICE_OFFLINE_STATUS,
    SaveFamilyAlarm,
    SaveFamilyAuthError,
    SaveFamilyConnectionError,
    SaveFamilyError,
    SaveFamilyHTTPError,
    SaveFamilyResponseError,
    SaveFamilyWatch,
    SaveFamilyWatchState,
    coerce_accuracy,
    coerce_float,
    coerce_int,
)

_LOGGER = logging.getLogger(__name__)

API_BASE = "https://api-platform.pro.savefamily.app/gateway/api"
WEBSOCKET_URL = "wss://api-platform.pro.savefamily.app/websocket"
PORTAL_ORIGIN = "https://portal.savefamily.app"
ACCESS_COOKIE = "token"
DEFAULT_2FA_METHOD = "EMAIL"
LOGIN_PLATFORM = "web"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Refresh the access token a little before the server expires it.
TOKEN_EXPIRY_MARGIN = 60
# Access-token lifetime when a token response omits `expiresIn` (the server sends 900).
DEFAULT_ACCESS_LIFETIME = 900.0
# Refresh rejections that are not about the token: a timeout and rate limiting.
_TRANSIENT_CLIENT_ERRORS = (408, 429)
# Contacts and safe zones change rarely; read them this often (seconds), not on every poll.
LISTS_REFRESH_INTERVAL = 1800
# Safety cap on `/devices/` pages; a family account has one.
MAX_DEVICE_PAGES = 20
# Position fields that identify nearby Wi-Fi networks; never kept in entity state.
_PRIVATE_POSITION_KEYS = ("networks", "message", "address")
# Connect alert types mapped onto the event types Classic already uses.
ALERT_TYPES = {
    "sos": "sos",
    "lowBattery": "low_battery",
    "geofenceIn": "geofence_enter",
    "geofenceOut": "geofence_exit",
    "disconnect": ALARM_TYPE_DISCONNECT,
    "reconnect": ALARM_TYPE_RECONNECT,
}
# Alerts have no id: theirs is the creation time in ms times this, plus the type's rank, so two
# alerts created in the same millisecond stay distinct. SOS ranks highest: if another alert of the
# same millisecond was seen first, an SOS listed only at the next poll still compares greater.
_ALERT_RANK = {name: rank for rank, name in enumerate(("reconnect", "disconnect", "lowBattery", "geofenceIn", "geofenceOut", "sos"), 1)}
_ALERT_ID_FACTOR = 8
# The integration's (Classic) command codes mapped to Connect command names.
COMMANDS = {
    COMMAND_REQUEST_LOCATION: "POSITION_REQUEST",
    COMMAND_FIND_WATCH: "FIND_DEVICE",
    COMMAND_RESTART: "RESTART",
    COMMAND_POWER_OFF: "SHUTDOWN",
}


def _criteria(**criteria: Any) -> str:
    """Query string for list endpoints: the portal sends orderBy/filters as base64 JSON."""
    params = {
        key: base64.b64encode(json.dumps(value).encode()).decode() if isinstance(value, (list, dict)) else value
        for key, value in criteria.items()
    }
    # base64 can contain `+`, `/` and `=`, which must not reach the server raw.
    return "?" + urlencode(params)


class SaveFamilyConnectCodeError(SaveFamilyAuthError):
    """The emailed login code was rejected."""


class SaveFamilyConnectClient:
    """Connect client exposing the same surface the coordinator uses for Classic."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        email: str,
        refresh_token: str | None = None,
        on_refresh_token: Callable[[str], None] | None = None,
    ) -> None:
        self._session = session
        self.email = email
        self._refresh_token = refresh_token
        self._on_refresh_token = on_refresh_token
        self._access_token: str | None = None
        self._access_expires_at = 0.0
        self._twofa_token: str | None = None
        self._twofa_method = DEFAULT_2FA_METHOD
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None
        # Access token of the last websocket attempt, and the last one obtained because the websocket refused one.
        self._ws_token: str | None = None
        self._ws_forced_token: str | None = None
        # From the last device list, keyed by watch did (identificator).
        self._watches: dict[str, SaveFamilyWatch] = {}
        self._connected: dict[str, bool | None] = {}
        # did -> (monotonic time read, SOS contact count, safe zone count).
        self._lists: dict[str, tuple[float, int | None, int | None]] = {}

    @property
    def refresh_token(self) -> str | None:
        return self._refresh_token

    @property
    def login_method(self) -> str:
        """Where the login code was sent (e.g. EMAIL), once the password step ran."""
        return self._twofa_method

    # --- login (config flow) -------------------------------------------------

    async def async_start_login(self, password: str) -> str:
        """Check email/password and have the server send a login code; return its method."""
        data = await self._request(
            "POST", "/auth/login", json={"email": self.email, "password": password}, auth=False
        )
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise SaveFamilyError("login response has no 2FA session")
        methods = [m for m in data.get("availableMethods") or [] if isinstance(m, dict)]
        default = next((m for m in methods if m.get("isDefault")), methods[0] if methods else {})
        self._twofa_token = token
        self._twofa_method = default.get("methodType") or DEFAULT_2FA_METHOD
        await self._request(
            "POST",
            "/auth/2fa/request-code",
            json={"token": token, "methodType": self._twofa_method},
            auth=False,
            bare_ok=True,
        )
        return self._twofa_method

    async def async_finish_login(self, code: str) -> None:
        """Exchange the emailed code for tokens."""
        if self._twofa_token is None:
            raise SaveFamilyError("login code requested before the password step")
        try:
            data = await self._request(
                "POST",
                "/auth/twofa/login",
                json={
                    "token": self._twofa_token,
                    "code": code.strip(),
                    "methodType": self._twofa_method,
                    "platform": LOGIN_PLATFORM,
                    "rememberMe": True,
                },
                auth=False,
            )
        except SaveFamilyAuthError as exc:
            raise SaveFamilyConnectCodeError(str(exc)) from exc
        self._store_tokens(data)

    # --- session ---------------------------------------------------------------

    async def async_refresh_for_websocket(self) -> bool:
        """Refresh the access token the websocket handshake refused; at most once per token.

        Return False when the refused token was already one obtained this way: the websocket
        refuses a fresh token too, so refreshing again would only rotate the session.
        """
        async with self._lock:
            if self._ws_token is None or self._ws_token == self._ws_forced_token:
                return False
            await self._async_refresh_access_token()
            self._ws_forced_token = self._access_token
            return True

    async def async_ws_connect(self, **kwargs: Any) -> aiohttp.ClientWebSocketResponse:
        """Open the push websocket; like the portal, it authenticates with the `token` cookie."""
        await self._async_ensure_access_token()
        self._ws_token = self._access_token
        headers = {"Origin": PORTAL_ORIGIN, "Cookie": f"{ACCESS_COOKIE}={self._access_token}"}
        return await self._session.ws_connect(WEBSOCKET_URL, headers=headers, heartbeat=30, **kwargs)

    async def _async_ensure_access_token(self) -> None:
        async with self._lock:
            if self._access_token and time.monotonic() < self._access_expires_at:
                return
            await self._async_refresh_access_token()

    async def _async_refresh_access_token(self) -> None:
        if not self._refresh_token:
            raise SaveFamilyAuthError("no Connect session; log in again")
        # The refresh and the storage of the rotated token run in one task that cancelling the caller
        # (unload, shutdown) does not interrupt: a token the server has rotated must reach the entry.
        if self._refresh_task is None:
            self._refresh_task = asyncio.ensure_future(self._async_do_refresh())
            self._refresh_task.add_done_callback(self._refresh_done)
        await asyncio.shield(self._refresh_task)

    async def _async_do_refresh(self) -> None:
        try:
            data = await self._request(
                "POST", "/auth/refresh", json={"refreshToken": self._refresh_token}, auth=False
            )
        except SaveFamilyHTTPError as exc:
            # The server answers a rejected refresh token with 401; any other client error means
            # the same, while a timeout, rate limiting or a server error is retried at the next poll.
            if 400 <= exc.status < 500 and exc.status not in _TRANSIENT_CLIENT_ERRORS:
                raise SaveFamilyAuthError(f"refresh token rejected: {exc}") from exc
            raise
        self._store_tokens(data)

    def _refresh_done(self, task: asyncio.Task[None]) -> None:
        self._refresh_task = None
        if not task.cancelled():
            # Retrieved here too, for when the caller that started it was cancelled.
            task.exception()

    async def async_wait_for_token_refresh(self) -> None:
        """Let a refresh in progress finish and store its token (unload, shutdown)."""
        if (task := self._refresh_task) is not None:
            await asyncio.wait([task])

    def _store_tokens(self, data: dict[str, Any]) -> None:
        access = data.get("accessToken")
        refresh = data.get("refreshToken")
        if not isinstance(access, str) or not isinstance(refresh, str):
            raise SaveFamilyError("token response is missing accessToken or refreshToken")
        lifetime = coerce_float(data.get("expiresIn"))
        if lifetime is None or lifetime <= 0:
            lifetime = DEFAULT_ACCESS_LIFETIME
        self._access_token = access
        self._access_expires_at = time.monotonic() + max(lifetime - TOKEN_EXPIRY_MARGIN, 0)
        if refresh != self._refresh_token:
            self._refresh_token = refresh
            if self._on_refresh_token is not None:
                self._on_refresh_token(refresh)

    # --- data ------------------------------------------------------------------

    async def async_refresh_watch_states(
        self,
        previous: dict[str, SaveFamilyWatchState] | None = None,
        skip: Container[str] = (),
    ) -> dict[str, SaveFamilyWatchState]:
        """State of every watch of the account; those in skip are listed but not read."""
        previous = previous or {}
        states: dict[str, SaveFamilyWatchState] = {}
        for device in await self._async_list_devices():
            if not isinstance(device, dict) or not device.get("id"):
                continue
            watch = _watch_from_device(device)
            if watch.did in skip:
                states[watch.did] = SaveFamilyWatchState(watch=watch)
                continue
            try:
                # The device list already proved the session, so a 401/403 here is about
                # this watch only (e.g. limited sharing) and must not end the session.
                latest = await self._authed(
                    "GET", f"/devices/{watch.did_id}/positions/latest", retry_auth=False
                )
            except SaveFamilyError as exc:
                _LOGGER.debug("Latest position of %s failed: %s", watch.did, exc)
                latest = {}
            states[watch.did] = _state_from_position(watch, device, latest.get("item"), previous.get(watch.did))
        if not states:
            # Like Classic: an empty list keeps the last data instead of removing every device.
            raise SaveFamilyResponseError(None, "no watches were found on this SaveFamily Connect account", {})
        for did, state in states.items():
            if did in skip:
                continue
            sos_contacts, safe_zones = await self._async_list_counts(state.watch)
            states[did] = replace(state, sos_contacts=sos_contacts, safe_zones=safe_zones)
        self._watches = {did: state.watch for did, state in states.items()}
        self._connected = {did: state.connected for did, state in states.items()}
        return states

    async def _async_list_counts(self, watch: SaveFamilyWatch) -> tuple[int | None, int | None]:
        """SOS contact and safe zone counts, re-read every LISTS_REFRESH_INTERVAL.

        Only counts are kept: contact names and numbers, and zone places, stay out of Home Assistant.
        """
        read_at, sos_contacts, safe_zones = self._lists.get(watch.did, (None, None, None))
        if read_at is not None and time.monotonic() - read_at < LISTS_REFRESH_INTERVAL:
            return sos_contacts, safe_zones
        try:
            contact_lists = _items(
                await self._authed("GET", f"/devices/{watch.did_id}/contact-lists", retry_auth=False), "contact lists"
            )
            geofence_page = await self._authed("GET", f"/devices/{watch.did_id}/geofences", retry_auth=False)
            geofences = _items(geofence_page, "geofences")
        except SaveFamilyError as exc:
            # Keep the previous counts and try again on the next poll.
            _LOGGER.debug("Contacts or safe zones of %s failed: %s", watch.did, exc)
            return sos_contacts, safe_zones
        sos_contacts = sum(
            len(item.get("contacts") or [])
            for item in contact_lists
            if isinstance(item, dict) and item.get("type") == "emergency"
        )
        # The list is paged: `total` counts them all.
        safe_zones = coerce_int(geofence_page.get("total"))
        if safe_zones is None:
            safe_zones = len(geofences)
        self._lists[watch.did] = (time.monotonic(), sos_contacts, safe_zones)
        return sos_contacts, safe_zones

    async def _async_list_devices(self) -> list[Any]:
        """All pages of `GET /devices/` (`{total, items, page, pages}`)."""
        items: list[Any] = []
        page = 1
        while True:
            data = await self._authed("GET", "/devices/" if page == 1 else f"/devices/?page={page}")
            items.extend(data.get("items") or [])
            pages = coerce_int(data.get("pages")) or 1
            if page >= min(pages, MAX_DEVICE_PAGES) or not data.get("items"):
                return items
            page += 1

    async def async_find_alarms(self, did: str) -> list[SaveFamilyAlarm]:
        """The newest alerts (SOS, low battery, zone in/out, disconnect/reconnect), oldest first."""
        if (watch := self._watches.get(did)) is None:
            return []
        device_id = watch.did_id
        query = _criteria(
            page=1,
            pageSize=ALARM_FETCH_COUNT,
            orderBy=[{"field": "createdAt", "sortDirection": "DESC"}],
        )
        try:
            data = await self._authed("GET", f"/devices/{device_id}/alerts{query}", retry_auth=False)
        except SaveFamilyAuthError as exc:
            # As with positions: the device list proved the session, so this is about this watch's
            # rights only. A plain error makes the coordinator skip alerts instead of asking to log in.
            raise SaveFamilyError(f"alerts of {did} are not accessible: {exc}") from exc
        alarms = [alarm for item in _items(data, "alerts") if (alarm := _alarm_from_alert(item))]
        return sorted(alarms, key=lambda alarm: alarm.id)

    def supports_command(self, did: str, command: str) -> bool:
        watch = self._watches.get(did)
        return watch is not None and COMMANDS.get(command) in watch.supported_commands

    async def async_send_command(
        self, did: str, command: str, *params: str, with_model: bool = False
    ) -> dict[str, Any]:
        """Send a command to the watch through `POST /commands` (body as the portal sends it)."""
        name = COMMANDS.get(command)
        if name is None or not self.supports_command(did, command):
            raise SaveFamilyError("This watch command is not supported on SaveFamily Connect yet")
        if self._connected.get(did) is False:
            # The portal disables commands while the watch is disconnected.
            raise SaveFamilyResponseError(DEVICE_OFFLINE_STATUS, DEVICE_OFFLINE_MESSAGE, {})
        # An action: it may answer a bare "OK", like the login code request.
        return await self._authed("POST", "/commands", json={"device": did, "command": name, "data": {}}, bare_ok=True)

    async def _authed(
        self, method: str, path: str, *, retry_auth: bool = True, **kwargs: Any
    ) -> dict[str, Any]:
        await self._async_ensure_access_token()
        try:
            return await self._request(method, path, **kwargs)
        except SaveFamilyAuthError:
            if not retry_auth:
                raise
            # Access token revoked early: one refresh and retry.
            async with self._lock:
                await self._async_refresh_access_token()
            return await self._request(method, path, **kwargs)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        auth: bool = True,
        bare_ok: bool = False,
    ) -> dict[str, Any]:
        """JSON body of a 2xx answer; bare_ok accepts a non-JSON one (an action answering "OK")."""
        request_headers = {
            "Accept": "application/json",
            "Origin": PORTAL_ORIGIN,
            "Referer": f"{PORTAL_ORIGIN}/",
            **(headers or {}),
        }
        if auth and self._access_token:
            # The gateway reads the access token from the portal's `token` cookie.
            request_headers["Authorization"] = f"Bearer {self._access_token}"
            request_headers["Cookie"] = f"{ACCESS_COOKIE}={self._access_token}"
        try:
            async with self._session.request(
                method,
                f"{API_BASE}{path}",
                json=json,
                headers=request_headers,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                text = await response.text(errors="replace")
                status = response.status
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise SaveFamilyConnectionError(f"{method} {path}: {exc}") from exc

        _LOGGER.debug("Connect %s %s -> %s", method, path, status)
        if status in (401, 403):
            raise SaveFamilyAuthError(f"{method} {path}: HTTP {status} {_error_message(text)}")
        if status >= 400:
            raise SaveFamilyHTTPError(status, f"{method} {path}: {_error_message(text)}")
        try:
            payload = json_loads(text)
        except ValueError:
            if bare_ok:
                return {}
            # A maintenance page or an empty body: not an answer to read data from.
            raise SaveFamilyResponseError(None, f"{method} {path}: not JSON: {_error_message(text)}", {}) from None
        return payload if isinstance(payload, dict) else {"items": payload}


def _items(data: dict[str, Any], what: str) -> list[Any]:
    """The `items` of a list answer; any other answer must not read as an empty list."""
    items = data.get("items")
    if not isinstance(items, list):
        raise SaveFamilyResponseError(None, f"{what} answer has no items list", {})
    return items


def _error_message(text: str) -> str:
    return text[:200]


def _timestamp(value: Any) -> datetime | None:
    """Connect timestamps are epoch milliseconds; anything unparseable is None."""
    millis = coerce_float(value)
    if millis is None or millis <= 0:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _supported_commands(device: dict[str, Any]) -> tuple[str, ...]:
    capabilities = device.get("capabilities") if isinstance(device.get("capabilities"), dict) else {}
    commands = capabilities.get("commands") if isinstance(capabilities.get("commands"), dict) else {}
    if commands.get("enabled") is False:
        return ()
    return tuple(name for name in commands.get("types") or [] if isinstance(name, str))


def _watch_from_device(device: dict[str, Any]) -> SaveFamilyWatch:
    return SaveFamilyWatch(
        supported_commands=_supported_commands(device),
        did=str(device.get("identificator") or device["id"]),
        did_id=str(device["id"]),
        model=str(device.get("type") or ""),
        nickname=str(device.get("carrierName") or ""),
        rolename="",
        watch_type=str(device.get("protocol") or ""),
    )


def _flag(flags: dict[str, Any], key: str) -> bool | None:
    value = flags.get(key)
    return value if isinstance(value, bool) else None


def _device_fields(device: dict[str, Any]) -> dict[str, Any]:
    """State fields read from the device itself rather than from its latest position."""
    flags = device.get("flags") if isinstance(device.get("flags"), dict) else {}
    settings = device.get("settings") if isinstance(device.get("settings"), dict) else {}
    disconnected = _flag(flags, "isDisconnect")
    in_or_out = flags.get("isInOrOut")
    sound_mode = settings.get("soundMode")
    return {
        "battery": coerce_int(device.get("battery")),
        "connected": None if disconnected is None else not disconnected,
        "battery_low": _flag(flags, "isBatteryLow"),
        "in_safe_zone": {"in": True, "out": False}.get(in_or_out) if isinstance(in_or_out, str) else None,
        "last_connection": _timestamp(device.get("lastConnection")),
        "reporting_interval": coerce_int(settings.get("frequency")),
        "sound_mode": sound_mode.lower() if isinstance(sound_mode, str) and sound_mode else None,
    }


def _alarm_from_alert(item: Any) -> SaveFamilyAlarm | None:
    """One alert; newer alerts get greater ids (see _ALERT_RANK)."""
    if not isinstance(item, dict):
        return None
    created_ms = coerce_int(item.get("createdAt"))
    if not created_ms or created_ms < 0:
        return None
    alert_type = item.get("type") if isinstance(item.get("type"), str) else ""
    return SaveFamilyAlarm(
        id=created_ms * _ALERT_ID_FACTOR + _ALERT_RANK.get(alert_type, 0),
        type=ALERT_TYPES.get(alert_type, ALARM_TYPE_OTHER),
        type_code=None,
        # deviceName is the child's name: not kept.
        message="",
        created=_timestamp(item.get("reportedAt") or created_ms),
    )


def _state_from_position(
    watch: SaveFamilyWatch,
    device: dict[str, Any],
    position: Any,
    previous: SaveFamilyWatchState | None,
) -> SaveFamilyWatchState:
    fields = _device_fields(device)
    if not isinstance(position, dict):
        position = {}
    latitude = coerce_float(position.get("latitude"))
    longitude = coerce_float(position.get("longitude"))
    if latitude is None or longitude is None or (latitude == 0 and longitude == 0):
        # No position, or the (0, 0) "no fix" sentinel: keep the last one, like Classic, rather
        # than clearing the tracker or placing the watch in the Gulf of Guinea.
        return replace(previous or SaveFamilyWatchState(watch=watch), watch=watch, **fields)
    return SaveFamilyWatchState(
        watch=watch,
        latitude=latitude,
        longitude=longitude,
        accuracy=coerce_accuracy(position.get("hpe")),
        step_count=coerce_int(position.get("steps")),
        last_fix=_timestamp(position.get("positionDate")),
        raw_position={k: v for k, v in position.items() if k not in _PRIVATE_POSITION_KEYS},
        **fields,
    )
