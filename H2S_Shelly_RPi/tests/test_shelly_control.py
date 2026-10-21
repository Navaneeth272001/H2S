import asyncio

import aiohttp

from src.shelly_control import execute_battery_decision
from src.shelly_rpc import ShellyRPCClient


class FakeShellyRPC:
    def __init__(self):
        self.states = {1: False, 2: True}
        self.writes = []

    async def read_device(self, session, device_id, device):
        state = self.states[device_id]
        return {
            "device_id": device_id,
            "device_name": device["name"],
            "ip": device["ip"],
            "identity": {"model": "mock"},
            "relay_state": state,
            "measurement_component": device.get("measurement_component", "switch:0"),
            "power_w": 100.0 if state else 0.0,
            "current_a": 0.5 if state else 0.0,
            "voltage_v": 230.0,
            "energy_total_wh": None,
            "measurement_timestamp": "2026-10-21T10:00:00Z",
            "measurement_quality": "OK",
        }

    async def set_relay_verified(self, session, device_id, device, target_on):
        before = await self.read_device(session, device_id, device)
        sent = before["relay_state"] != target_on
        if sent:
            self.writes.append((device_id, target_on))
            self.states[device_id] = target_on
        after = await self.read_device(session, device_id, device)
        return {
            "device_id": device_id,
            "name": device["name"],
            "ip": device["ip"],
            "switch_id": device.get("switch_id", 0),
            "target_state": target_on,
            "state_before": before["relay_state"],
            "command_sent": sent,
            "command_acknowledged": True if sent else None,
            "state_after": after["relay_state"],
            "verified": after["relay_state"] is target_on,
            "measurement_before_w": before["power_w"],
            "measurement_after_w": after["power_w"],
            "measurement_component": before["measurement_component"],
            "measurement_quality": after["measurement_quality"],
            "command_timestamp": "2026-10-21T10:00:00Z" if sent else None,
            "observed_timestamp": after["measurement_timestamp"],
            "latency_ms": 12.0,
        }


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=None, history=(), status=self.status, message="mock error"
            )

    async def json(self):
        return self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        return FakeResponse(*self.responses.pop(0))


DEVICES = {
    1: {"name": "priority", "ip": "192.0.2.1", "role": "priority", "switch_id": 0},
    2: {"name": "noncritical", "ip": "192.0.2.2", "role": "non_critical", "switch_id": 1},
}


def run(coro):
    return asyncio.run(coro)


def test_discharge_targets_only_priority_and_verifies_effect():
    rpc = FakeShellyRPC()
    result = run(execute_battery_decision(
        None, "DISCHARGE", DEVICES, rpc_client=rpc,
        execution_mode="hardware", physical_commands_enabled=True,
    ))
    assert result["status"] == "COMPLETED"
    assert rpc.writes == [(1, True)]
    assert result["commands"][0]["verified"] is True
    assert result["consumption_effect"]["status"] == "EXPECTED_LOAD_EFFECT_OBSERVED"


def test_charge_is_load_reduction_not_battery_charge():
    rpc = FakeShellyRPC()
    result = run(execute_battery_decision(
        None, "CHARGE", DEVICES, rpc_client=rpc,
        execution_mode="hardware", physical_commands_enabled=True,
    ))
    assert result["status"] == "COMPLETED"
    assert rpc.writes == [(2, False)]
    assert result["physical_battery_charge"] == "SIMULATED_LOAD_REDUCTION_ONLY"


def test_standby_does_not_read_or_write_relays():
    rpc = FakeShellyRPC()
    result = run(execute_battery_decision(None, "STANDBY", DEVICES, rpc_client=rpc,
                                          execution_mode="hardware", physical_commands_enabled=True))
    assert result["status"] == "MAINTAINED"
    assert rpc.writes == []
    assert result["commands"] == []


def test_simulation_and_dry_run_never_send_relay_commands():
    rpc = FakeShellyRPC()
    simulated = run(execute_battery_decision(None, "DISCHARGE", DEVICES, rpc_client=rpc,
                                             execution_mode="simulation"))
    dry_run = run(execute_battery_decision(None, "DISCHARGE", DEVICES, rpc_client=rpc,
                                           execution_mode="dry_run"))
    assert simulated["status"] == "SIMULATED"
    assert dry_run["status"] == "DRY_RUN"
    assert rpc.writes == []


def test_hardware_requires_explicit_enable_and_invalid_action_is_rejected():
    denied = run(execute_battery_decision(None, "CHARGE", DEVICES, execution_mode="hardware"))
    invalid = run(execute_battery_decision(None, "TOGGLE", DEVICES, execution_mode="hardware",
                                           physical_commands_enabled=True))
    assert denied["status"] == "REJECTED"
    assert invalid["status"] == "REJECTED"


def test_status_parser_keeps_missing_measurements_unavailable():
    parsed = ShellyRPCClient.parse_device(
        8, {"ip": "192.0.2.8", "switch_id": 0, "measurement_component": "em:0"},
        {"model": "mock"}, {"switch:0": {"output": False}, "em:0": {"total_act_power": 45.3}},
    )
    assert parsed["relay_state"] is False
    assert parsed["power_w"] == 45.3
    assert parsed["current_a"] is None
    assert parsed["energy_total_wh"] is None


def test_rpc_retries_transient_server_failure_with_bounded_attempts():
    client = ShellyRPCClient(timeout=1, max_retries=1)
    session = FakeSession([(503, {}), (200, {"switch:0": {"output": True}})])
    result = run(client._request(session, {"ip": "192.0.2.1"}, "Shelly.GetStatus"))
    assert result["switch:0"]["output"] is True
    assert session.calls == 2


def test_missing_measurement_values_are_not_emitted_as_zero():
    parsed = ShellyRPCClient.parse_device(
        8, {"ip": "192.0.2.8", "switch_id": 0, "measurement_component": "switch:0"},
        {"model": "mock"}, {"switch:0": {"output": False, "apower": None}},
    )
    assert parsed["power_w"] is None
    assert parsed["measurement_quality"] == "UNAVAILABLE"
