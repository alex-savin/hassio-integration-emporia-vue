from __future__ import annotations

from typing import Any, Iterable, TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, SIGNAL_SNAPSHOT_UPDATE

if TYPE_CHECKING:
    from . import EmporiaState


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    state: EmporiaState = data["state"]
    entry_uid: str = data.get("entry_id", entry.entry_id)

    devices = _merge_devices_by_gid(state.devices or [])
    entities: list[SensorEntity] = []
    for device in devices:
        entities.append(EmporiaDeviceSensor(entry_uid, state, device))
        entities.append(EmporiaUsageSensor(entry_uid, state, device))
        entities.append(EmporiaUsagePowerSensor(entry_uid, state, device))
        for ch in device.get("channels") or []:
            entities.append(EmporiaChannelUsageSensor(entry_uid, state, device, ch))
            entities.append(
                EmporiaChannelUsagePowerSensor(entry_uid, state, device, ch)
            )

    async_add_entities(entities)


def _root_devices(devices: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dev for dev in devices if dev.get("parentDeviceGid") is None]


def _flatten_devices(devices: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for dev in devices:
        flat.append(dev)
        nested = dev.get("devices") or []
        if nested:
            flat.extend(_flatten_devices(nested))
    return flat


def _merge_devices_by_gid(devices: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge multiple device entries sharing the same GID, combining channels."""

    merged: dict[int, dict[str, Any]] = {}
    for dev in _flatten_devices(devices):
        gid = dev.get("deviceGid")
        if gid is None:
            continue

        existing = merged.get(gid)
        if existing is None:
            copy = dict(dev)
            copy["channels"] = list(dev.get("channels") or [])
            merged[gid] = copy
            continue

        channels = existing.get("channels") or []
        seen_nums = {str(ch.get("channelNum")) for ch in channels}
        for ch in dev.get("channels") or []:
            num = str(ch.get("channelNum"))
            if num not in seen_nums:
                channels.append(ch)
                seen_nums.add(num)
        existing["channels"] = channels

        if (
            existing.get("deviceConnected") is None
            and dev.get("deviceConnected") is not None
        ):
            existing["deviceConnected"] = dev.get("deviceConnected")
        if not existing.get("locationProperties") and dev.get("locationProperties"):
            existing["locationProperties"] = dev.get("locationProperties")

    return list(merged.values())


def energy_to_watts(energy_kwh: Any) -> float | None:
    """Convert 1-minute interval energy (kWh) to average watts."""
    try:
        energy = float(energy_kwh)
    except (TypeError, ValueError):
        return None

    # 1MIN scale: window is 1/60 hour
    kw = energy / (1 / 60)
    return round(kw * 1000, 3)


def round_energy(val: float) -> float:
    """Round interval kWh to a reasonable precision for display/logging."""
    return round(val, 4)


def is_connected(dev: dict[str, Any] | None) -> bool:
    """Return connection status, defaulting to connected when unknown."""
    status = (dev or {}).get("deviceConnected") or {}
    if status.get("connected") is True:
        return True
    if status.get("connected") is False:
        return False
    return True


def channel_icon(channel: dict[str, Any]) -> str:
    """Return a suitable icon for a channel, special-casing mains aggregation."""
    chan_num = str(channel.get("channelNum"))
    chan_type = str(channel.get("type") or "").lower()

    if "," in chan_num or chan_type == "main":
        return "mdi:home-lightning-bolt"
    return "mdi:lightning-bolt"


class EmporiaDeviceSensor(SensorEntity):
    _attr_icon = "mdi:power-plug"

    def __init__(
        self, entry_uid: str, state: EmporiaState, device: dict[str, Any]
    ) -> None:
        self._state = state
        self._device_gid = device.get("deviceGid")
        self._name = self._device_name(device)
        self._attr_unique_id = f"{entry_uid}-{self._device_gid}-connection"
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
        """Subscribe to updates."""
        signal = f"{SIGNAL_SNAPSHOT_UPDATE}_{self._state.entry_id}"
        self._unsub_update = async_dispatcher_connect(
            self.hass, signal, self._handle_update
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from updates."""
        if self._unsub_update:
            self._unsub_update()
            self._unsub_update = None

    @callback
    def _handle_update(self) -> None:
        """Handle state update."""
        self.async_write_ha_state()

    @property
    def name(self) -> str:
        return f"{self._name} Connection"

    @property
    def native_value(self) -> str:
        dev = self._current_device()
        if not dev:
            return STATE_UNKNOWN
        status = dev.get("deviceConnected") or {}
        if status.get("connected") is True:
            return STATE_ON
        if status.get("connected") is False:
            return STATE_OFF
        return STATE_UNKNOWN

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        dev = self._current_device() or {}
        return {
            "device_gid": dev.get("deviceGid"),
            "model": dev.get("model"),
            "firmware": dev.get("firmware"),
            "has_outlet": dev.get("outlet") is not None,
            "has_ev_charger": dev.get("evCharger") is not None,
            "channel_count": len(dev.get("channels") or []),
        }

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


class EmporiaUsageSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        entry_uid: str,
        state: EmporiaState,
        device: dict[str, Any],
    ) -> None:
        self._state = state
        self._device_gid = device.get("deviceGid")
        self._name = self._device_name(device)
        self._attr_icon = "mdi:lightning-bolt"
        self._attr_unique_id = f"{entry_uid}-{self._device_gid}-usage-1min"
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
        return "Usage (1 min)"

    @property
    def native_value(self) -> float | None:
        val = self._state.device_totals.get(self._device_gid)
        if val is None:
            return None
        return round_energy(max(0.0, float(val)))

    @property
    def native_unit_of_measurement(self) -> str | None:
        unit = self._state.energy_unit
        if unit == "KilowattHours":
            return UnitOfEnergy.KILO_WATT_HOUR
        return unit

    @property
    def available(self) -> bool:
        dev = self._current_device() or {}
        return is_connected(dev)

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


class EmporiaUsagePowerSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        entry_uid: str,
        state: EmporiaState,
        device: dict[str, Any],
    ) -> None:
        self._state = state
        self._device_gid = device.get("deviceGid")
        self._name = self._device_name(device)
        self._attr_icon = "mdi:flash"
        self._attr_unique_id = f"{entry_uid}-{self._device_gid}-power-1min"
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
        return "Power (1 min)"

    @property
    def native_value(self) -> float | None:
        energy = self._state.device_interval.get(self._device_gid)
        return energy_to_watts(energy)

    @property
    def native_unit_of_measurement(self) -> str | None:
        return UnitOfPower.WATT

    @property
    def available(self) -> bool:
        dev = self._current_device() or {}
        return is_connected(dev)

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


class EmporiaChannelUsageSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        entry_uid: str,
        state: EmporiaState,
        device: dict[str, Any],
        channel: dict[str, Any],
    ) -> None:
        self._state = state
        self._device_gid = device.get("deviceGid")
        self._channel_num = str(channel.get("channelNum"))
        self._channel_name = channel.get("name") or f"Channel {self._channel_num}"
        self._attr_icon = channel_icon(channel)
        base_name = self._device_name(device)
        self._name = f"{base_name} {self._channel_name}"
        self._attr_unique_id = (
            f"{entry_uid}-{self._device_gid}-{self._channel_num}-usage-1min"
        )
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
        return f"{self._channel_name} Usage (1 min)"

    @property
    def native_value(self) -> float | None:
        key = f"{self._device_gid}:{self._channel_num}"
        val = self._state.channel_totals.get(key)
        if val is None:
            return None
        return round_energy(max(0.0, float(val)))

    @property
    def native_unit_of_measurement(self) -> str | None:
        unit = self._state.energy_unit
        if unit == "KilowattHours":
            return UnitOfEnergy.KILO_WATT_HOUR
        return unit

    @property
    def available(self) -> bool:
        dev = self._current_device() or {}
        return is_connected(dev)

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


class EmporiaChannelUsagePowerSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        entry_uid: str,
        state: EmporiaState,
        device: dict[str, Any],
        channel: dict[str, Any],
    ) -> None:
        self._state = state
        self._device_gid = device.get("deviceGid")
        self._channel_num = str(channel.get("channelNum"))
        self._channel_name = channel.get("name") or f"Channel {self._channel_num}"
        self._attr_icon = "mdi:flash"
        base_name = self._device_name(device)
        self._name = f"{base_name} {self._channel_name}"
        self._attr_unique_id = (
            f"{entry_uid}-{self._device_gid}-{self._channel_num}-power-1min"
        )
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
        return f"{self._channel_name} Power (1 min)"

    @property
    def native_value(self) -> float | None:
        key = f"{self._device_gid}:{self._channel_num}"
        energy = self._state.channel_interval.get(key)
        return energy_to_watts(energy)

    @property
    def native_unit_of_measurement(self) -> str | None:
        return UnitOfPower.WATT

    @property
    def available(self) -> bool:
        dev = self._current_device() or {}
        return is_connected(dev)

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
