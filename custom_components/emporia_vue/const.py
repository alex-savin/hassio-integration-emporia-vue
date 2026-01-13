from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "emporia_vue"
DEFAULT_WS_URL = "ws://homeassistant.local:8080/ws"
DEFAULT_SCALES: list[str] = ["1MIN"]
USAGE_SCALES: list[str] = ["1MIN", "1D", "1M"]

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH]

# Dispatcher signal for push updates
SIGNAL_SNAPSHOT_UPDATE = f"{DOMAIN}_snapshot_update"
