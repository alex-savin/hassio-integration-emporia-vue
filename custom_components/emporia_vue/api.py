from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable
from uuid import uuid4

import aiohttp

_LOGGER = logging.getLogger(__name__)


class EmporiaError(Exception):
    """Base exception for Emporia integration."""


class EmporiaAuthError(EmporiaError):
    """Authentication failed."""


class EmporiaClient:
    """Lightweight WebSocket client for the add-on bridge with push support."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        ws_url: str,
        username: str,
        password: str,
    ) -> None:
        self._session = session
        self._ws_url = ws_url
        self._username = username
        self._password = password
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._hello: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self._authenticated = False
        self._listener_task: asyncio.Task | None = None
        self._snapshot_callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._listen_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()

    @property
    def url(self) -> str:
        return self._ws_url

    def register_snapshot_callback(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Register a callback for push snapshots. Returns unsubscribe function."""
        self._snapshot_callbacks.append(callback)

        def unsubscribe():
            if callback in self._snapshot_callbacks:
                self._snapshot_callbacks.remove(callback)

        return unsubscribe

    async def _ensure_ws(self) -> aiohttp.ClientWebSocketResponse:
        if self._ws and not self._ws.closed:
            return self._ws

        _LOGGER.debug("Connecting to Emporia WS %s", self.url)
        self._ws = await self._session.ws_connect(self.url, heartbeat=30)

        # Read greeting
        hello_msg = await self._ws.receive_json()
        self._hello = hello_msg
        self._authenticated = False
        _LOGGER.debug(
            "Received hello: type=%s version=%s capabilities=%s",
            hello_msg.get("type"),
            hello_msg.get("version"),
            hello_msg.get("capabilities"),
        )
        return self._ws

    async def _send_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        ws = await self._ensure_ws()

        # Avoid deadlock: perform authentication before taking the send/receive lock.
        if payload.get("type") != "authenticate" and not self._authenticated:
            await self.async_authenticate()

        async with self._lock:
            # In case authentication flipped while waiting for the lock.
            if payload.get("type") != "authenticate" and not self._authenticated:
                await self.async_authenticate()

            request_id = str(uuid4())
            message = {**payload, "request_id": request_id}
            _LOGGER.debug("-> %s", message)
            await ws.send_json(message)

            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=15)
                except asyncio.TimeoutError:
                    _LOGGER.debug(
                        "<- websocket receive timed out; closing and resetting auth"
                    )
                    await ws.close()
                    self._ws = None
                    self._authenticated = False
                    raise EmporiaError("websocket receive timeout")

                if msg.type == aiohttp.WSMsgType.CLOSED:
                    _LOGGER.debug("<- websocket closed: code=%s", ws.close_code)
                    self._ws = None
                    self._authenticated = False
                    raise EmporiaError("websocket closed")
                if msg.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.debug("<- websocket error: %s", ws.exception())
                    self._ws = None
                    self._authenticated = False
                    raise EmporiaError(f"websocket error: {ws.exception()}")
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue

                try:
                    data: dict[str, Any] = json.loads(msg.data)
                except json.JSONDecodeError:
                    _LOGGER.debug("<- (non-json) %s", msg.data)
                    continue

                _LOGGER.debug("<- %s", data)
                if data.get("request_id") == request_id or (
                    payload.get("type") == "authenticate"
                    and data.get("type") in {"auth_ok", "error"}
                ):
                    if data.get("type") == "error":
                        if data.get("code") == "unauthorized":
                            self._authenticated = False
                            raise EmporiaAuthError(data.get("message", "unauthorized"))
                        raise EmporiaError(data.get("message", "unknown error"))
                    return data

    async def start_listener(self) -> None:
        """Start persistent WebSocket listener for push-mode."""
        if self._listener_task is not None:
            return

        self._stop_event.clear()
        self._listener_task = asyncio.create_task(self._listener_loop())
        _LOGGER.info("Started persistent WebSocket listener")

    async def stop_listener(self) -> None:
        """Stop persistent WebSocket listener."""
        self._stop_event.set()
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None
        _LOGGER.info("Stopped persistent WebSocket listener")

    async def _listener_loop(self) -> None:
        """Persistent loop that listens for push snapshots and handles requests."""
        reconnect_delay = 5
        max_reconnect_delay = 60

        while not self._stop_event.is_set():
            try:
                async with self._listen_lock:
                    ws = await self._ensure_ws()

                    # Authenticate
                    if not self._authenticated:
                        auth_msg = {
                            "type": "authenticate",
                            "request_id": str(uuid4()),
                            "username": self._username,
                            "password": self._password,
                        }
                        await ws.send_json(auth_msg)

                        # Wait for auth response
                        while True:
                            msg = await asyncio.wait_for(ws.receive(), timeout=30)
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                continue
                            data = json.loads(msg.data)
                            if data.get("type") == "auth_ok":
                                self._authenticated = True
                                _LOGGER.info("WebSocket listener authenticated")
                                break
                            elif data.get("type") == "error":
                                raise EmporiaAuthError(
                                    data.get("message", "auth failed")
                                )

                # Successfully connected, reset delay
                reconnect_delay = 5

                # Main receive loop
                while not self._stop_event.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=120)
                    except asyncio.TimeoutError:
                        # Send ping to keep alive
                        await ws.send_json({"type": "ping"})
                        continue

                    if msg.type == aiohttp.WSMsgType.CLOSED:
                        _LOGGER.warning("WebSocket closed, reconnecting...")
                        break
                    if msg.type == aiohttp.WSMsgType.ERROR:
                        _LOGGER.warning("WebSocket error: %s", ws.exception())
                        break
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue

                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue

                    msg_type = data.get("type")

                    # Handle push snapshots
                    if msg_type == "snapshot":
                        _LOGGER.debug(
                            "Received snapshot: %d devices, %d usage entries",
                            len(data.get("devices") or []),
                            len(
                                (data.get("usage") or {})
                                .get("deviceListUsages", {})
                                .get("devices", [])
                            ),
                        )
                        for cb in self._snapshot_callbacks:
                            try:
                                cb(data)
                            except Exception as err:
                                _LOGGER.error("Snapshot callback error: %s", err)

                    # Handle responses to pending requests
                    request_id = data.get("request_id")
                    if request_id and request_id in self._pending_requests:
                        self._pending_requests[request_id].set_result(data)

            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.warning(
                    "WebSocket listener error: %s, reconnecting in %ds",
                    err,
                    reconnect_delay,
                )
                self._ws = None
                self._authenticated = False
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

    async def async_send_with_listener(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a request via the persistent listener connection."""
        if not self._listener_task or not self._ws or self._ws.closed:
            # Fall back to request-response mode
            return await self._send_request(payload)

        request_id = str(uuid4())
        message = {**payload, "request_id": request_id}

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_requests[request_id] = future

        try:
            async with self._lock:
                await self._ws.send_json(message)

            return await asyncio.wait_for(future, timeout=15)
        finally:
            self._pending_requests.pop(request_id, None)

    async def async_authenticate(self) -> None:
        resp = await self._send_request(
            {
                "type": "authenticate",
                "username": self._username,
                "password": self._password,
            }
        )
        if resp.get("type") != "auth_ok":
            raise EmporiaAuthError("authentication failed")
        self._authenticated = True
        _LOGGER.debug("authentication succeeded")

    async def async_get_devices(self) -> list[dict[str, Any]]:
        resp = await self._send_request({"type": "get_devices"})
        devices = resp.get("devices", [])
        if not isinstance(devices, list):
            raise EmporiaError("invalid devices payload")
        return devices

    async def async_toggle_outlet(self, device_gid: int, on: bool) -> None:
        payload = {"type": "toggle_outlet", "device_gid": device_gid, "on": on}
        resp = await self._send_request(payload)
        if resp.get("type") != "toggle_outlet_ack":
            raise EmporiaError("unexpected toggle_outlet response")

    async def async_toggle_evse(self, device_gid: int, on: bool) -> None:
        payload = {"type": "toggle_evse", "device_gid": device_gid, "on": on}
        resp = await self._send_request(payload)
        if resp.get("type") != "toggle_evse_ack":
            raise EmporiaError("unexpected toggle_evse response")

    async def async_get_usage(
        self, device_gids: list[int] | None = None, scale: str = "1MIN"
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "get_usage",
            "scale": scale,
            "unit": "KilowattHours",
        }
        if device_gids:
            payload["device_gids"] = device_gids
        resp = await self._send_request(payload)
        if resp.get("type") != "usage":
            raise EmporiaError("unexpected usage response")
        return resp

    async def async_close(self) -> None:
        await self.stop_listener()
        if self._ws and not self._ws.closed:
            _LOGGER.debug("closing websocket connection")
            await self._ws.close()
            self._ws = None

    async def async_ping(self) -> bool:
        """Send a ping to validate the websocket connection."""

        try:
            resp = await self._send_request({"type": "ping"})
        except Exception as err:  # pragma: no cover - debug helper
            _LOGGER.debug("ping failed: %s", err)
            return False
        _LOGGER.debug("ping ok: %s", resp)
        return True
