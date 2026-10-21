# Shelly Control Audit and Commissioning Notes

## Repository audit

- `src/config.py` contains the collector's Shelly IP list and hard-coded priority roles.
- `src/collector.py::fetch_shelly` polls `Switch.GetStatus?id=0`; it currently substitutes zero for missing power/current/voltage fields.
- `main_collector.py` polls Shelly devices once per `POLL_INTERVAL`, stores minute measurements in `EnergyMinute`, integrates hourly energy, and publishes per-device telemetry.
- `src/mqtt_publisher.py` publishes per-device readings to `h2s/agents/<AGENT_UUID>/telemetrie`, receives `ordres`, and publishes execution reports to `comptes-rendus`.
- `main_collector.py::_handle_ordre` maps an MQTT order's `action`/`command` to `execute_battery_decision`.
- `src/shelly_control.py` maps `DISCHARGE` to priority relays ON, `CHARGE` to non-critical relays OFF, and `STANDBY` to no action. It does not verify observed state.
- `src/battery.py::BatteryManager` simulates SoC and labels the result simulated; physical telemetry is an independent Socketry read path. Shelly relays do not measure battery SoC or prove battery charging.
- `src/database.py` stores per-minute measured Shelly power and simulated or physical battery readings. Existing MQTT contracts remain in place.
- Deployment is via `h2s-collector.service` or `docker-compose.yml`.

## Implementation plan and protocol choice

Use the repository's existing local Shelly Gen2/3 JSON-RPC assumption (`Shelly.GetDeviceInfo`, `Shelly.GetStatus`, `Switch.Set`) behind a reusable adapter. Device identity is inspected at runtime; unsupported generations, missing channels, and absent measurements are reported as unavailable. Since the actual device models and firmware are not present in this repository, commissioning must confirm that each installed device supports this RPC interface. MQTT control is not added because it would require device-specific broker setup and is not needed for the documented local API path.

The H2S decision remains the input. A shared execution gate selects `simulation`, `dry_run`, or `hardware`; hardware commands require an explicit enable flag. Device role, switch channel, and measurement component can be supplied in `SHELLY_DEVICES_JSON`. Commands are idempotent, bounded-retry, state-verified, and report measured before/after power only when the configured component supplies it. Missing readings remain unavailable. `CHARGE` is reported as a simulated charging scenario because Shelly switching only changes appliance load; it does not command or measure battery charging.

## Hardware dependencies and limits

- Confirm each Shelly model, generation, firmware, IP, channel, measurement component, and wiring before enabling `hardware` mode.
- Shelly consumption measurements describe the circuit/component monitored by that Shelly. They do not establish battery SoC or battery charge power.
- The expected-load feedback is a before/after observation around the relay command, not a guarantee that only the relay caused the change. An effect is marked unavailable when suitable power measurements are absent.
- Option A Socketry remains a separate read-only telemetry integration. Direct Jackery charging/discharging control is not implemented or implied.
