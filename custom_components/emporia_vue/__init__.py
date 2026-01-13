from __future__ import annotations

import logging
import asyncio
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import aiohttp_client
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.storage import Store
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .api import EmporiaClient, EmporiaAuthError, EmporiaError
from .const import DEFAULT_WS_URL, DOMAIN, PLATFORMS, SIGNAL_SNAPSHOT_UPDATE

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration via YAML (not supported)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Emporia Vue from a config entry."""
    data = entry.data
    ws_url: str = data.get("ws_url", DEFAULT_WS_URL)
    username: str = data["username"]
    password: str = data["password"]

    _LOGGER.debug(
        "setting up config entry %s (ws_url=%s)",
        entry.entry_id,
        ws_url,
    )

    session = aiohttp_client.async_get_clientsession(hass)
    client = EmporiaClient(
        session=session,
        ws_url=ws_url,
        username=username,
        password=password,
    )

    try:
        await client.async_authenticate()
    except EmporiaAuthError as err:
        _LOGGER.error("Auth failed during setup: %s", err)
        return False
    except EmporiaError as err:
        _LOGGER.error("Setup failed: %s", err)
        return False

    # Get initial devices
    try:
        devices = await client.async_get_devices()
    except EmporiaError as err:
        _LOGGER.error("Failed to get devices: %s", err)
        raise ConfigEntryNotReady("Device fetch failed") from err

    device_ids = _device_ids_flat(devices)
    channel_keys = _channel_keys(devices)

    # Initialize shared state
    state = EmporiaState(hass, entry.entry_id, devices, device_ids, channel_keys)

    # Restore cached data
    await state.restore_from_cache()

    # Register snapshot callback
    @callback
    def on_snapshot(snapshot: dict[str, Any]) -> None:
        """Handle incoming push snapshot."""
        hass.loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                _process_snapshot(hass, entry.entry_id, state, snapshot)
            )
        )

    unsubscribe = client.register_snapshot_callback(on_snapshot)

    # Start persistent listener
    await client.start_listener()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "state": state,
        "unsubscribe_snapshot": unsubscribe,
        "entry_id": entry.entry_id,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Schedule periodic cache persistence
    async def periodic_persist():
        while True:
            await asyncio.sleep(300)  # Every 5 minutes
            await state.persist_to_cache()

    persist_task = asyncio.create_task(periodic_persist())
    hass.data[DOMAIN][entry.entry_id]["persist_task"] = persist_task

    return True


async def _process_snapshot(
    hass: HomeAssistant, entry_id: str, state: "EmporiaState", snapshot: dict[str, Any]
) -> None:
    """Process an incoming snapshot and update state."""
    try:
        # Update devices if present
        devices = snapshot.get("devices")
        if devices:
            state.devices = devices
            state.device_ids = _device_ids_flat(devices)
            state.channel_keys = _channel_keys(devices)

        # Update usage data
        usage = snapshot.get("usage", {})
        usage_data = usage.get("deviceListUsages", {})
        devices_usage = usage_data.get("devices", [])
        state.energy_unit = usage_data.get("energyUnit")
        ts = snapshot.get("timestamp", datetime.utcnow().isoformat())
        if isinstance(ts, str):
            state.last_update = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            state.last_update = datetime.utcnow()

        # Process usage and accumulate totals
        for dev in devices_usage:
            gid = dev.get("deviceGid")
            if gid is None:
                continue
            gid_int = int(gid)
            channels = dev.get("channelUsages") or []
            per_device_sum = 0.0
            for ch in channels:
                try:
                    usage_val = max(0.0, float(ch.get("usage", 0)))
                    chan_num = str(ch.get("channelNum") or "")
                    key = f"{gid_int}:{chan_num}"
                    state.channel_totals[key] = (
                        state.channel_totals.get(key, 0.0) + usage_val
                    )
                    state.channel_interval[key] = usage_val
                    # Skip main/aggregated channels (comma-separated) for device total
                    # to avoid double-counting house usage
                    if "," not in chan_num:
                        per_device_sum += usage_val
                        state.device_totals[gid_int] = (
                            state.device_totals.get(gid_int, 0.0) + usage_val
                        )
                except (TypeError, ValueError):
                    continue
            state.device_interval[gid_int] = per_device_sum

        # Fire dispatcher signal to update all sensors
        async_dispatcher_send(hass, f"{SIGNAL_SNAPSHOT_UPDATE}_{entry_id}")

        _LOGGER.debug(
            "Processed snapshot: %d devices, %d channels",
            len(state.device_totals),
            len(state.channel_totals),
        )
    except Exception as err:
        _LOGGER.error("Error processing snapshot: %s", err)


class EmporiaState:
    """Shared state for Emporia Vue data."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        devices: list[dict[str, Any]],
        device_ids: list[int],
        channel_keys: list[str],
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.devices = devices
        self.device_ids = device_ids
        self.channel_keys = channel_keys
        self.device_totals: dict[int, float] = {}
        self.device_interval: dict[int, float] = {}
        self.channel_totals: dict[str, float] = {}
        self.channel_interval: dict[str, float] = {}
        self.energy_unit: str | None = None
        self.last_update: datetime | None = None
        self._device_store = Store(hass, 1, f"{DOMAIN}_{entry_id}_devices.json")
        self._usage_store = Store(hass, 1, f"{DOMAIN}_{entry_id}_usage.json")

    async def restore_from_cache(self) -> None:
        """Restore cached data from disk."""
        # Restore devices
        cached_devices = await self._device_store.async_load()
        if cached_devices:
            self.devices = cached_devices
            self.device_ids = _device_ids_flat(cached_devices)
            self.channel_keys = _channel_keys(cached_devices)
            _LOGGER.debug("Restored %d devices from cache", len(self.devices))

        # Restore usage totals
        cached_usage = await self._usage_store.async_load()
        if cached_usage:
            self.device_totals = {
                int(k): float(v) for k, v in cached_usage.get("device", {}).items()
            }
            self.channel_totals = {
                str(k): float(v) for k, v in cached_usage.get("channel", {}).items()
            }
            self.energy_unit = cached_usage.get("energy_unit")
            _LOGGER.debug(
                "Restored usage from cache: %d devices, %d channels",
                len(self.device_totals),
                len(self.channel_totals),
            )

    async def persist_to_cache(self) -> None:
        """Persist current data to disk."""
        await self._device_store.async_save(self.devices)
        await self._usage_store.async_save(
            {
                "device": self.device_totals,
                "channel": self.channel_totals,
                "energy_unit": self.energy_unit,
            }
        )
        _LOGGER.debug("Persisted data to cache")


def _device_ids_flat(devices: list[dict]) -> list[int]:
    seen: set[int] = set()

    def walk(items: list[dict]):
        for dev in items:
            gid = dev.get("deviceGid")
            if gid is not None and gid not in seen:
                seen.add(int(gid))
            nested = dev.get("devices") or []
            if nested:
                walk(nested)

    walk(devices)
    return list(seen)


def _channel_keys(devices: list[dict]) -> list[str]:
    keys: set[str] = set()

    def walk(items: list[dict]):
        for dev in items:
            gid = dev.get("deviceGid")
            for ch in dev.get("channels") or []:
                num = ch.get("channelNum")
                if gid is not None and num is not None:
                    keys.add(f"{int(gid)}:{num}")
            nested = dev.get("devices") or []
            if nested:
                walk(nested)

    walk(devices or [])
    return list(keys)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    stored = hass.data.get(DOMAIN, {}).pop(entry.entry_id, {})
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Cancel persist task
    persist_task = stored.get("persist_task")
    if persist_task:
        persist_task.cancel()
        try:
            await persist_task
        except asyncio.CancelledError:
            pass

    # Unsubscribe from snapshots
    unsubscribe = stored.get("unsubscribe_snapshot")
    if unsubscribe:
        unsubscribe()

    # Close client
    client: EmporiaClient | None = stored.get("client")
    if client:
        await client.async_close()

    # Final cache persist
    state: EmporiaState | None = stored.get("state")
    if state:
        await state.persist_to_cache()

    return unload_ok
