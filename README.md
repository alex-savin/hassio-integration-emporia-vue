# Emporia Vue Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Validate](https://github.com/alex-savin/hassio-integration-emporia-vue/actions/workflows/validate.yml/badge.svg)](https://github.com/alex-savin/hassio-integration-emporia-vue/actions/workflows/validate.yml)
[![Lint](https://github.com/alex-savin/hassio-integration-emporia-vue/actions/workflows/lint.yml/badge.svg)](https://github.com/alex-savin/hassio-integration-emporia-vue/actions/workflows/lint.yml)

Custom Home Assistant integration that connects to the Emporia Vue WebSocket bridge add-on. Inspired by [ha-emporia-vue](https://github.com/magico13/ha-emporia-vue) but designed for local communication via the WebSocket add-on.

## Prerequisites

This integration requires the **[Emporia Vue WebSocket Add-on](https://github.com/alex-savin/hassio-apps/tree/main/emporia-ws)** to be installed and running.

The add-on connects to Emporia's servers, authenticates with your credentials, and exposes device data via a local websocket. This integration then connects to that websocket to create Home Assistant entities.

### Add-on Installation

1. Add the add-on repository to Home Assistant:
   - Go to **Settings → Add-ons → Add-on Store**
   - Click the three dots (⋮) in the top right → **Repositories**
   - Add: `https://github.com/alex-savin/hassio-apps`
2. Find and install **Emporia Vue WebSocket**
3. Configure the add-on with your Emporia credentials
4. Start the add-on
5. Note the websocket URL (typically `ws://homeassistant.local:8080/ws`)

For detailed add-on configuration and endpoints, see the [add-on documentation](https://github.com/alex-savin/hassio-apps/tree/main/emporia-ws).

## Features

- Config flow with add-on host/port, Emporia username/password, and optional WS auth token
- WebSocket client that authenticates to the add-on and polls devices every 5 minutes
- **Sensors**: per-device connection status, per-device usage (sum of channels), per-channel usage (energy), with device registry entries
- **Switches**: outlets and EV chargers (on/off)

## Installation

### HACS (Recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=alex-savin&repository=hassio-integration-emporia-vue&category=integration)

1. Open HACS in Home Assistant
2. Click "Integrations"
3. Click the three dots in the top right corner
4. Select "Custom repositories"
5. Add this repository URL: `https://github.com/alex-savin/hassio-integration-emporia-vue`
6. Select "Integration" as the category
7. Click "Add"
8. Search for "Emporia Vue" and install it
9. Restart Home Assistant

### Manual Installation

1. Copy `custom_components/emporia_vue/` into your Home Assistant `custom_components/` directory
2. Restart Home Assistant
3. Go to Settings → Devices & Services → Add Integration
4. Search for "Emporia Vue" and follow the setup wizard

## Configuration

During setup, you'll need to provide:

| Field | Description |
|-------|-------------|
| **WS URL** | Full websocket endpoint (default `ws://homeassistant.local:8080/ws`) |
| **Username** | Your Emporia account username |
| **Password** | Your Emporia account password |
| **WS Auth Token** | Optional shared secret (`WS_AUTH_TOKEN`) configured on the add-on |

## Entities

### Sensors (per device)
- Device connection status
- Total device usage (sum of all channels)
- Per-channel energy usage

### Switches
- Outlet on/off control
- EV charger on/off control

## Automation Examples

### Alert on high energy usage
```yaml
alias: High energy usage alert
trigger:
  - platform: numeric_state
    entity_id: sensor.emporia_vue_total_usage
    above: 5000
action:
  - service: notify.mobile_app_your_phone
    data:
      title: "High Energy Usage"
      message: "Current usage is {{ states('sensor.emporia_vue_total_usage') }}W"
```

### Turn off EV charger during peak hours
```yaml
alias: EV charger peak hours
trigger:
  - platform: time
    at: "16:00:00"
action:
  - service: switch.turn_off
    target:
      entity_id: switch.emporia_vue_ev_charger
```

## Notes / TODO

- Usage coordinator currently uses the device list captured at setup; add dynamic refresh if devices change
- Consider adding services for EVSE rate settings and channel control if needed

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
