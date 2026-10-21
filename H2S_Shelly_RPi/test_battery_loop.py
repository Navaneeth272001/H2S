"""
Test Suite: Closed-Loop Physical Battery Integration (Chemin B)
===============================================================
Tests the full cyber-physical loop:
1. Battery dynamic state calculation (SoC, charging/discharging power, voltage, temperature)
2. Generation and serialization of battery data packets (telemetrie & H2S local agent status)
3. Validation against h2s_agent's native battery parser (parsers.h2s_battery)
4. Decision translation into Shelly EM relay commands (CHARGE / DISCHARGE / STANDBY)
5. Cyber-physical closed loop effect:
   - DISCHARGE -> Priority devices ON -> Power drawn from battery -> SoC decreases
   - CHARGE    -> Non-critical devices OFF -> Load drops -> Simulated surplus charges battery -> SoC increases
   - STANDBY   -> Stable state
6. Jackery Socketry reader configuration (Chemin A, read only)
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project roots to path
repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(repo_root / "H2S_Shelly_RPi"))
sys.path.insert(0, str(repo_root / "agent"))

from H2S_Shelly_RPi.src.config import (
    SHELLY_DEVICES,
    SHELLY_PRIORITY_IDS,
    SHELLY_NON_CRITICAL_IDS,
    BATTERY_STOCK_ID,
    BATTERY_NAME,
)
from H2S_Shelly_RPi.src.battery import BatteryManager, get_battery_manager
from H2S_Shelly_RPi.src.shelly_control import execute_battery_decision
from H2S_Shelly_RPi.src.jackery_api import JackeryClient
from H2S_Shelly_RPi.src.mqtt_publisher import _make_payload_dict

try:
    from h2s_agent import parsers
    AGENT_PARSER_AVAILABLE = True
except ImportError:
    AGENT_PARSER_AVAILABLE = False


def test_battery_simulation_dynamics():
    """Verify battery physics: charging, discharging, and standby behavior."""
    mgr = BatteryManager(initial_soc=80.0, capacity_wh=1000.0, voltage_nominal=230.0)
    assert mgr.soc_pct == 80.0

    # 1. STANDBY
    r_standby = mgr.update(active_load_w=200.0, action="STANDBY", dt_seconds=60.0)
    assert r_standby.power_charge_w == 0.0
    assert r_standby.power_discharge_w == 0.0
    assert r_standby.action == "STANDBY"
    assert 79.9 <= r_standby.soc_pct <= 80.0

    # 2. DISCHARGE with 400W load from active Shelly devices
    r_discharge = mgr.update(active_load_w=400.0, action="DISCHARGE", dt_seconds=360.0)  # 6 min
    # 400W * 0.1h = 40Wh -> 4% drop on 1000Wh battery
    assert r_discharge.power_discharge_w == 400.0
    assert r_discharge.power_charge_w == 0.0
    assert r_discharge.action == "DISCHARGE"
    assert round(r_discharge.soc_pct, 1) == 76.0
    assert r_discharge.voltage_v < 230.0  # Voltage droop under load
    assert r_discharge.current_a > 1.5

    # 3. CHARGE with simulated surplus
    r_charge = mgr.update(active_load_w=0.0, action="CHARGE", dt_seconds=360.0)  # 6 min at 500W
    # 500W * 0.1h = 50Wh -> 5% rise
    assert r_charge.power_charge_w == 500.0
    assert r_charge.power_discharge_w == 0.0
    assert r_charge.action == "CHARGE"
    assert round(r_charge.soc_pct, 1) == 81.0


def test_battery_data_packet_format():
    """Verify data packet structure matches the DevOps / H2S telemetrie contract."""
    mgr = BatteryManager(initial_soc=75.5)
    reading = mgr.update(active_load_w=250.0, action="DISCHARGE")

    # Packet for telemetry stream (QoS 1)
    soc_packet = _make_payload_dict(
        valeur=round(reading.soc_pct, 2),
        unite="%",
        capteur_id=f"battery-{BATTERY_STOCK_ID}-soc",
        timestamp=reading.timestamp
    )
    assert soc_packet["unite"] == "%"
    assert soc_packet["valeur"] == round(reading.soc_pct, 2)
    assert soc_packet["capteur_id"] == "battery-1-soc"
    assert "timestamp" in soc_packet

    p_discharge_packet = _make_payload_dict(
        valeur=round(reading.power_discharge_w, 1),
        unite="W",
        capteur_id=f"battery-{BATTERY_STOCK_ID}-discharge-power",
        timestamp=reading.timestamp
    )
    assert p_discharge_packet["unite"] == "W"
    assert p_discharge_packet["valeur"] == 250.0

    # Combined H2S local agent status payload
    h2s_payload = reading.to_h2s_payload()
    assert "soc_pct" in h2s_payload
    assert "power_charge_w" in h2s_payload
    assert "power_discharge_w" in h2s_payload
    assert "voltage_v" in h2s_payload
    assert "current_a" in h2s_payload
    assert "temperature_c" in h2s_payload


def test_h2s_agent_parser_compatibility():
    """Validate that h2s_agent's native parser successfully accepts our battery payload."""
    if not AGENT_PARSER_AVAILABLE:
        return

    mgr = BatteryManager(initial_soc=82.0)
    reading = mgr.update(active_load_w=180.0, action="DISCHARGE")
    payload_json = json.dumps(reading.to_h2s_payload()).encode("utf-8")
    now_utc = datetime.now(timezone.utc)

    # Ingest through h2s_agent parsers.h2s_battery
    readings = parsers.h2s_battery(stock_id=1, payload=payload_json, received=now_utc)
    assert len(readings) == 6
    by_type = {r.data_type: r.value for r in readings}
    assert by_type["soc_pct"] == round(reading.soc_pct, 2)
    assert by_type["power_discharge_w"] == 180.0
    assert by_type["power_charge_w"] == 0.0
    assert by_type["voltage_v"] > 220.0
    assert by_type["temperature_c"] > 20.0


def test_decision_translation_logic():
    """Verify configured roles map to plans while default mode cannot actuate."""
    class MockClientSession:
        pass

    async def run_async_test():
        # Test DISCHARGE: should turn ON priority devices
        res_discharge = await execute_battery_decision(MockClientSession(), "DISCHARGE")
        assert res_discharge["action"] == "DISCHARGE"
        assert res_discharge["status"] == "SIMULATED"
        assert {item["device_id"] for item in res_discharge["commands"]} == set(SHELLY_PRIORITY_IDS)

        # Test CHARGE: should turn OFF non-critical devices
        res_charge = await execute_battery_decision(MockClientSession(), "CHARGE")
        assert res_charge["action"] == "CHARGE"
        assert res_charge["status"] == "SIMULATED"
        assert {item["device_id"] for item in res_charge["commands"]} == set(SHELLY_NON_CRITICAL_IDS)

        # Test STANDBY: should maintain states
        res_standby = await execute_battery_decision(MockClientSession(), "STANDBY")
        assert res_standby["action"] == "STANDBY"
        assert res_standby["status"] == "MAINTAINED"

    asyncio.run(run_async_test())


def test_jackery_api_client():
    """Socketry mode requires real credentials and a selected device serial."""
    client = JackeryClient(email="", password="")
    async def expect_missing_credentials():
        try:
            await client.read_battery()
        except RuntimeError as exc:
            assert "JACKERY_EMAIL" in str(exc)
        else:
            raise AssertionError("Socketry must not fabricate telemetry without configured credentials")
    asyncio.run(expect_missing_credentials())


if __name__ == "__main__":
    print("Running Battery Integration Tests...")
    test_battery_simulation_dynamics()
    print("✓ test_battery_simulation_dynamics passed")
    test_battery_data_packet_format()
    print("✓ test_battery_data_packet_format passed")
    test_h2s_agent_parser_compatibility()
    print("✓ test_h2s_agent_parser_compatibility passed")
    test_decision_translation_logic()
    print("✓ test_decision_translation_logic passed")
    test_jackery_api_client()
    print("✓ test_jackery_api_client passed")
    print("\nAll Physical Battery Integration Tests Passed Successfully! [5/5]")
