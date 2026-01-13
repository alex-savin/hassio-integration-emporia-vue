from __future__ import annotations

from typing import Any, Iterable, TYPE_CHECKING

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .api import EmporiaClient, EmporiaError
from .const import DOMAIN, SIGNAL_SNAPSHOT_UPDATE

if TYPE_CHECKING:
    from . import EmporiaState


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    state: EmporiaState = data["state"]
    client: EmporiaClient = data["client"]

    devices = state.devices or []
    entities: list[SwitchEntity] = []
    for device in _flatten_devices(devices):
        if device.get("outlet"):
            entities.append(EmporiaOutletSwitch(state, client, device))
        if device.get("evCharger"):
            entities.append(EmporiaEvseSwitch(state, client, device))

    async_add_entities(entities)


def _flatten_devices(devices: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for dev in devices:
        flat.append(dev)
        nested = dev.get("devices") or []
        if nested:
            flat.extend(_flatten_devices(nested))
    return flat


class EmporiaOutletSwitch(SwitchEntity):
    _attr_has_entity_name = True

    def __init__(
        self, state: EmporiaState, client: EmporiaClient, device: dict[str, Any]
    ) -> None:
        self._state = state
        self._client = client
        self._device_gid = device.get("deviceGid")
        self._name = self._device_name(device)
        self._attr_unique_id = f"{state.entry_id}-{self._device_gid}-outlet"
        self._unsub_update: callable | None = None

    @staticmethod
    def _device_name(device: dict[str, Any]) -> str:
        props = device.get("locationProperties") or {}
        name = (
            props.get("deviceName")
            or device.get("model")
            or f"Device {device.get('deviceGid')}"
        )
        return str(name)

    def _current_device(self) -> dict[str, Any] | None:
        for dev in _flatten_devices(self._state.devices or []):
            if dev.get("deviceGid") == self._device_gid:
                return dev
        return None

    async def async_added_to_hass(self) -> None:
        signal = f"{SIGNAL_SNAPSHOT_UPDATE}_{self._state.entry_id}"
        self._unsub_update = async_dispatcher_connect(
            self.hass, signal, self._handle_update
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_update:
            self._unsub_update()
            self._unsub_update = None

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def name(self) -> str:
        return f"{self._name} Outlet"

    @property
    def is_on(self) -> bool:
        dev = self._current_device() or {}
        outlet = dev.get("outlet") or {}
        return outlet.get("outletOn") is True

    @property
    def available(self) -> bool:
        dev = self._current_device() or {}
        status = dev.get("deviceConnected") or {}
        return status.get("connected") is True

    @property
    def device_info(self) -> DeviceInfo:
        dev = self._current_device() or {}
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._device_gid))},
            name=self._device_name(dev) if dev else self._name,
            manufacturer="Emporia",
            model=str(dev.get("model")) if dev else None,
            sw_version=str(dev.get("firmware")) if dev else None,
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._toggle(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._toggle(False)

    async def _toggle(self, on: bool) -> None:
        try:
            await self._client.async_toggle_outlet(self._device_gid, on)
        except EmporiaError as err:
            self._attr_available = False
            raise err


class EmporiaEvseSwitch(SwitchEntity):
    _attr_has_entity_name = True

    def __init__(
        self, state: EmporiaState, client: EmporiaClient, device: dict[str, Any]
    ) -> None:
        self._state = state
        self._client = client
        self._device_gid = device.get("deviceGid")
        self._name = self._device_name(device)
        self._attr_unique_id = f"{state.entry_id}-{self._device_gid}-evse"
        self._unsub_update: callable | None = None

    @staticmethod
    def _device_name(device: dict[str, Any]) -> str:
        props = device.get("locationProperties") or {}
        name = (
            props.get("deviceName")
            or device.get("model")
            or f"Device {device.get('deviceGid')}"
        )
        return str(name)

    def _current_device(self) -> dict[str, Any] | None:
        for dev in _flatten_devices(self._state.devices or []):
            if dev.get("deviceGid") == self._device_gid:
                return dev
        return None

    async def async_added_to_hass(self) -> None:
        signal = f"{SIGNAL_SNAPSHOT_UPDATE}_{self._state.entry_id}"
        self._unsub_update = async_dispatcher_connect(
            self.hass, signal, self._handle_update
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_update:
            self._unsub_update()
            self._unsub_update = None

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def name(self) -> str:
        return f"{self._name} EV Charger"

    @property
    def is_on(self) -> bool:
        dev = self._current_device() or {}
        evse = dev.get("evCharger") or {}
        return evse.get("chargerOn") is True

    @property
    def available(self) -> bool:
        dev = self._current_device() or {}
        status = dev.get("deviceConnected") or {}
        return status.get("connected") is True

    @property
    def device_info(self) -> DeviceInfo:
        dev = self._current_device() or {}
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._device_gid))},
            name=self._device_name(dev) if dev else self._name,
            manufacturer="Emporia",
            model=str(dev.get("model")) if dev else None,
            sw_version=str(dev.get("firmware")) if dev else None,
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._toggle(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._toggle(False)

    async def _toggle(self, on: bool) -> None:
        try:
            await self._client.async_toggle_evse(self._device_gid, on)
        except EmporiaError as err:
            self._attr_available = False
            raise err
