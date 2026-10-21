import asyncio
import aiohttp
import logging
import signal
import json
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select
from src.config import (
    SHELLY_DEVICES, POLL_INTERVAL, MINUTES_PER_HOUR, TIMEZONE, MAX_ERRORS, HEARTBEAT_INTERVAL,
    BATTERY_ENABLED, BATTERY_STOCK_ID, BATTERY_NAME, SHELLY_PRIORITY_IDS,
    BATTERY_SOURCE, BATTERY_SIMULATED_CHARGE_W
)
from src.database import init_db, AsyncSessionLocal, EnergyMinute, EnergyHourly, WeatherHourly, BatteryMinute, ShellyCommand
from src.collector import fetch_shelly, fetch_weather
from src.battery import get_battery_manager, estimate_simulated_production_w
from src.shelly_control import execute_battery_decision
from src.notifier import send_email
from src.mqtt_publisher import (
    publish_energy_minute, publish_energy_hourly,
    publish_weather_hourly, publish_etat,
    publish_battery_minute, publish_battery_status_h2s, publish_decision_report,
    publish_device_status_h2s,
    set_ordres_callback, get_mqtt_client, disconnect_mqtt
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("main_collector")

_main_loop = None
_http_session = None
_seen_order_ids = set()
_seen_order_ids_order = deque(maxlen=10000)
_seen_order_lock = threading.Lock()
_command_lock = asyncio.Lock()


def _as_datetime(value):
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


async def _persist_shelly_report(action: str, report: dict):
    commands = report.get("commands") or []
    errors = report.get("errors") or []
    async with AsyncSessionLocal() as db_session:
        db_session.add(ShellyCommand(
            correlation_id=report.get("correlation_id"), action=action,
            execution_mode=report.get("execution_mode", "unknown"),
            status=report.get("status", "UNKNOWN"),
            error=("; ".join(str(item.get("error", "")) for item in errors) or None),
            result_json=json.dumps(report, separators=(",", ":"), default=str),
            issued_at=_as_datetime(report.get("issued_at")),
        ))
        for item in commands:
            db_session.add(ShellyCommand(
                correlation_id=report.get("correlation_id"), action=action,
                execution_mode=report.get("execution_mode", "unknown"),
                status=report.get("status", "UNKNOWN"), device_id=item.get("device_id"),
                device_name=item.get("name"), switch_id=item.get("switch_id"),
                target_state=item.get("target_state"), command_sent=item.get("command_sent"),
                command_acknowledged=item.get("command_acknowledged"),
                state_before=item.get("state_before"), state_after=item.get("state_after"),
                verified=item.get("verified"), measurement_before_w=item.get("measurement_before_w"),
                measurement_after_w=item.get("measurement_after_w"),
                measurement_quality=item.get("measurement_quality"), latency_ms=item.get("latency_ms"),
                issued_at=_as_datetime(report.get("issued_at")),
                observed_at=_as_datetime(item.get("observed_timestamp")),
            ))
        for item in errors:
            db_session.add(ShellyCommand(
                correlation_id=report.get("correlation_id"), action=action,
                execution_mode=report.get("execution_mode", "unknown"),
                status=report.get("status", "FAILED"), device_id=item.get("device_id"),
                device_name=item.get("name"), target_state=item.get("target_state"),
                error=str(item.get("error", "command failed"))[:1000],
                issued_at=_as_datetime(report.get("issued_at")),
            ))
        if commands or errors:
            await db_session.commit()

def _handle_ordre(data: dict):
    """Callback for incoming cloud/agent orders on the ordres topic (or local decision)."""
    logger.info("Received battery order")
    # Extract action: directly or from inner payload
    action = None
    correlation_id = None
    expires_at = None
    if isinstance(data, dict):
        correlation_id = data.get("command_id") or data.get("correlation_id") or data.get("decision_id")
        expires_at = data.get("expires_at")
        if "action" in data:
            action = str(data["action"])
        elif "payload" in data:
            try:
                inner = json.loads(data["payload"]) if isinstance(data["payload"], str) else data["payload"]
                if not isinstance(inner, dict):
                    raise ValueError("order payload must be an object")
                action = inner.get("action")
                correlation_id = correlation_id or inner.get("command_id") or inner.get("decision_id")
                expires_at = expires_at or inner.get("expires_at")
            except Exception:
                pass
        elif "command" in data:
            action = str(data["command"])

    if action and _main_loop and _http_session:
        action_clean = action.upper().strip()
        if correlation_id:
            with _seen_order_lock:
                if correlation_id in _seen_order_ids:
                    publish_decision_report(action_clean, {"status": "DUPLICATE_IGNORED", "correlation_id": correlation_id})
                    logger.warning("Ignoring duplicate battery order %s", correlation_id)
                    return
                if len(_seen_order_ids_order) == _seen_order_ids_order.maxlen:
                    _seen_order_ids.discard(_seen_order_ids_order[0])
                _seen_order_ids_order.append(correlation_id)
                _seen_order_ids.add(correlation_id)
        expiry = _as_datetime(expires_at)
        if expires_at and (expiry is None or expiry <= datetime.now(timezone.utc)):
            report = {"status": "REJECTED_EXPIRED", "correlation_id": correlation_id,
                      "action": action_clean, "strategy": "shelly_indirect"}
            publish_decision_report(action_clean, report)
            logger.warning("Rejecting expired or invalid battery order %s", correlation_id)
            return
        logger.info(f"Dispatching decision action to Shelly relays (Chemin B): {action_clean}")
        coro = _execute_and_report_decision(action_clean, str(correlation_id) if correlation_id else str(uuid.uuid4()))
        asyncio.run_coroutine_threadsafe(coro, _main_loop)

async def _execute_and_report_decision(action: str, correlation_id: str | None = None):
    try:
        async with _command_lock:
            if correlation_id:
                async with AsyncSessionLocal() as db_session:
                    prior = await db_session.scalar(
                        select(ShellyCommand.id).where(
                            ShellyCommand.correlation_id == correlation_id,
                            ShellyCommand.device_id.is_(None),
                        ).limit(1)
                    )
                if prior is not None:
                    duplicate = {"status": "DUPLICATE_IGNORED", "correlation_id": correlation_id,
                                 "action": action, "strategy": "shelly_indirect"}
                    publish_decision_report(action, duplicate)
                    return
            battery_mgr = get_battery_manager()
            battery_mgr.set_action(action)
            report = await execute_battery_decision(
                _http_session, action, SHELLY_DEVICES, correlation_id=correlation_id
            )
            await _persist_shelly_report(action, report)
            publish_decision_report(action, report)
    except Exception as e:
        logger.error(f"Error executing battery decision {action}: {e}")


async def main():
    logger.info("Starting H2S Shelly & Weather Collector...")
    await init_db()

    # Initialise MQTT connection and subscribe to orders
    set_ordres_callback(_handle_ordre)
    get_mqtt_client()

    error_counts = {dev_id: 0 for dev_id in SHELLY_DEVICES}
    email_sent = {dev_id: False for dev_id in SHELLY_DEVICES}
    
    # buffers for hourly aggregation
    buffers = {dev_id: {"energy": 0.0, "p": 0.0, "i": 0.0, "v": 0.0, "c": 0, "ic": 0, "vc": 0} for dev_id in SHELLY_DEVICES}
    minute_count = 0
    heartbeat_elapsed = 0     # seconds since last etat publish
    
    tz = ZoneInfo(TIMEZONE)
    hour_start = datetime.now(tz).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)

    global _main_loop, _http_session
    _main_loop = asyncio.get_running_loop()
    battery_mgr = get_battery_manager()
    if BATTERY_ENABLED and BATTERY_SOURCE == "simulation":
        # Resume the digital twin's last simulated SoC across collector restarts.
        async with AsyncSessionLocal() as db_session:
            last_soc = await db_session.scalar(
                select(BatteryMinute.soc_pct)
                .where(BatteryMinute.is_simulated.is_(True))
                .order_by(BatteryMinute.timestamp.desc())
                .limit(1)
            )
        if last_soc is not None:
            battery_mgr.soc_pct = max(5.0, min(100.0, float(last_soc)))
            logger.info("Resumed simulated battery SoC at %.2f%%", battery_mgr.soc_pct)

    async with aiohttp.ClientSession() as session:
        _http_session = session
        weather_cache_hour = None
        weather_cache = None
        load_coverage_warning_active = False
        while True:
            now = datetime.now(tz)

            # The proposal's phase-1 source is weather plus simulated renewable
            # production. Refresh GHI hourly and reuse it for minute-level steps.
            current_hour = now.replace(minute=0, second=0, microsecond=0)
            if BATTERY_ENABLED and BATTERY_SOURCE == "simulation" and current_hour != weather_cache_hour:
                weather_cache_hour = current_hour
                weather_cache = await fetch_weather(session, current_hour)
                if weather_cache is None:
                    logger.warning("Weather unavailable; virtual renewable production is 0 W this hour")
                else:
                    logger.info("Weather GHI for simulated production: %.1f W/m²", weather_cache["ghi"])
            
            # Fetch all shellys concurrently
            tasks = [fetch_shelly(session, dev_id, dev_info) for dev_id, dev_info in SHELLY_DEVICES.items()]
            results = await asyncio.gather(*tasks)

            async with AsyncSessionLocal() as db_session:
                for res in results:
                    dev_id = res["device_id"]
                    if res["success"]:
                        error_counts[dev_id] = 0
                        email_sent[dev_id] = False
                        
                        power = res["power_w"]
                        current = res["current_a"]
                        voltage = res["voltage_v"]
                        energy_minute_wh = round(power * POLL_INTERVAL / 3600.0, 3) if power is not None else None

                        # Persist measurements actually supplied by the configured Shelly component.
                        em = EnergyMinute(
                            timestamp=now.replace(tzinfo=None),
                            device_id=dev_id,
                            device_name=res["device_name"],
                            current_a=current,
                            voltage_v=voltage,
                            power_w=power,
                            energy_minute_wh=energy_minute_wh,
                            relay_state=res.get("relay_state"),
                            measurement_quality=res.get("measurement_quality", "UNAVAILABLE"),
                            measurement_timestamp=_as_datetime(res.get("measurement_timestamp")),
                            energy_total_wh=res.get("energy_total_wh"),
                            shelly_model=(res.get("identity") or {}).get("model"),
                            shelly_firmware=(res.get("identity") or {}).get("fw_id")
                        )
                        db_session.add(em)

                        if any(value is not None for value in (power, current, voltage)):
                            publish_energy_minute(
                                device_id=dev_id,
                                device_name=res["device_name"],
                                current_a=current,
                                voltage_v=voltage,
                                power_w=power,
                                energy_minute_wh=energy_minute_wh,
                                timestamp=now
                            )
                            publish_device_status_h2s(
                                device_id=dev_id,
                                current_a=current,
                                voltage_v=voltage,
                                power_w=power,
                                energy_wh=energy_minute_wh,
                                timestamp=now
                            )

                        # Integrate energy only from measured active power.
                        if power is not None:
                            buffers[dev_id]["energy"] += energy_minute_wh
                            buffers[dev_id]["p"] += power
                            buffers[dev_id]["c"] += 1
                            if current is not None:
                                buffers[dev_id]["i"] += current
                                buffers[dev_id]["ic"] += 1
                            if voltage is not None:
                                buffers[dev_id]["v"] += voltage
                                buffers[dev_id]["vc"] += 1
                        elif res["measurement_quality"] == "UNAVAILABLE":
                            logger.warning("Shelly %s online but configured measurement %s is unavailable",
                                           dev_id, res["measurement_component"])
                        
                    else:
                        error_counts[dev_id] += 1
                        if error_counts[dev_id] >= MAX_ERRORS and not email_sent[dev_id]:
                            await send_email(
                                subject=f"🚨 Shelly Offline: {res['device_name']}",
                                message=f"Device ID: {dev_id} is unreachable."
                            )
                            email_sent[dev_id] = True

                # Process Battery measurements (Physical or Simulated — Chemin B)
                if BATTERY_ENABLED:
                    priority_active_load = sum(
                        res["power_w"] for res in results
                        if res.get("success") and res.get("device_id") in SHELLY_PRIORITY_IDS and res.get("power_w") is not None
                    )
                    measured_site_load = sum(
                        res["power_w"] for res in results
                        if res.get("success") and res.get("power_w") is not None
                    )
                    load_meter_ids = {
                        device_id for device_id, device in SHELLY_DEVICES.items()
                        if device.get("role") != "monitor_only"
                    }
                    measured_load_ids = {
                        res["device_id"] for res in results
                        if res.get("success") and res.get("power_w") is not None
                    }
                    site_load_complete = bool(load_meter_ids) and load_meter_ids.issubset(measured_load_ids)
                    if not site_load_complete and not load_coverage_warning_active:
                        logger.warning("Shelly load coverage incomplete; simulated CHARGE surplus set to 0 W")
                        load_coverage_warning_active = True
                    elif site_load_complete:
                        load_coverage_warning_active = False
                    simulated_production = estimate_simulated_production_w(
                        weather_cache.get("ghi") if weather_cache else None,
                        BATTERY_SIMULATED_CHARGE_W,
                    )
                    b_reading = await battery_mgr.update_async(
                        active_load_w=priority_active_load,
                        site_load_w=measured_site_load,
                        site_load_complete=site_load_complete,
                        simulated_production_w=simulated_production,
                        dt_seconds=POLL_INTERVAL,
                        timestamp=now
                    )

                    if b_reading is None:
                        logger.warning("Battery sample skipped; no physical reading was available.")
                    else:
                        bm = BatteryMinute(
                            timestamp=now.replace(tzinfo=None),
                            stock_id=b_reading.stock_id,
                            name=b_reading.name,
                            soc_pct=b_reading.soc_pct,
                            power_charge_w=b_reading.power_charge_w,
                            power_discharge_w=b_reading.power_discharge_w,
                            voltage_v=b_reading.voltage_v,
                            current_a=b_reading.current_a,
                            temperature_c=b_reading.temperature_c,
                            is_simulated=b_reading.is_simulated,
                            action=b_reading.action,
                            simulated_production_w=b_reading.simulated_production_w,
                            simulated_surplus_w=b_reading.simulated_surplus_w
                        )
                        db_session.add(bm)

                        # Publish battery telemetrie packet
                        publish_battery_minute(
                            stock_id=b_reading.stock_id,
                            battery_name=b_reading.name,
                            soc_pct=b_reading.soc_pct,
                            power_charge_w=b_reading.power_charge_w,
                            power_discharge_w=b_reading.power_discharge_w,
                            voltage_v=b_reading.voltage_v,
                            current_a=b_reading.current_a,
                            temperature_c=b_reading.temperature_c,
                            timestamp=now
                        )

                        # Publish battery status to local broker for h2s_agent
                        publish_battery_status_h2s(
                            stock_id=b_reading.stock_id,
                            soc_pct=b_reading.soc_pct,
                            power_charge_w=b_reading.power_charge_w,
                            power_discharge_w=b_reading.power_discharge_w,
                            voltage_v=b_reading.voltage_v,
                            current_a=b_reading.current_a,
                            temperature_c=b_reading.temperature_c,
                            timestamp=now
                        )

                await db_session.commit()
                
            logger.info(f"Minute data saved for {len(results)} devices and battery.")
            minute_count += 1
            
            # Hourly processing
            if minute_count >= MINUTES_PER_HOUR:
                hour_end = hour_start + timedelta(hours=1)
                
                async with AsyncSessionLocal() as db_session:
                    # Save Weather
                    weather_data = await fetch_weather(session, hour_start)
                    if weather_data:
                        wh = WeatherHourly(
                            timestamp=hour_start.replace(tzinfo=None),
                            temperature=weather_data["temperature"],
                            humidity=weather_data["humidity"],
                            wind_speed=weather_data["wind_speed"],
                            precipitation=weather_data["precipitation"],
                            condition=weather_data["condition"],
                            ghi=weather_data["ghi"]
                        )
                        db_session.add(wh)
                        logger.info("Weather data saved.")

                        # Publish weather to MQTT
                        publish_weather_hourly(
                            temperature=weather_data["temperature"],
                            humidity=weather_data["humidity"],
                            wind_speed=weather_data["wind_speed"],
                            precipitation=weather_data["precipitation"],
                            condition=weather_data["condition"],
                            ghi=weather_data["ghi"],
                            timestamp=hour_start
                        )
                    
                    # Save Hourly Energy
                    for dev_id, b in buffers.items():
                        if b["c"] > 0:
                            eh = EnergyHourly(
                                start_time=hour_start.replace(tzinfo=None),
                                end_time=hour_end.replace(tzinfo=None),
                                device_id=dev_id,
                                device_name=SHELLY_DEVICES[dev_id]["name"],
                                avg_current_a=round(b["i"] / b["ic"], 3) if b["ic"] else None,
                                avg_voltage_v=round(b["v"] / b["vc"], 1) if b["vc"] else None,
                                avg_power_w=round(b["p"] / b["c"], 1),
                                total_energy_wh=round(b["energy"], 2)
                            )
                            db_session.add(eh)

                            # Publish hourly energy to MQTT
                            publish_energy_hourly(
                                device_id=dev_id,
                                device_name=SHELLY_DEVICES[dev_id]["name"],
                                avg_current_a=round(b["i"] / b["ic"], 3) if b["ic"] else None,
                                avg_voltage_v=round(b["v"] / b["vc"], 1) if b["vc"] else None,
                                avg_power_w=round(b["p"] / b["c"], 1),
                                total_energy_wh=round(b["energy"], 2),
                                start_time=hour_start,
                                end_time=hour_end
                            )
                            
                    await db_session.commit()
                    logger.info("Hourly data committed.")
                
                # Reset
                buffers = {d: {"energy": 0.0, "p": 0.0, "i": 0.0, "v": 0.0, "c": 0, "ic": 0, "vc": 0} for d in SHELLY_DEVICES}
                minute_count = 0
                hour_start = hour_end

            # Heartbeat (etat) — publish every HEARTBEAT_INTERVAL seconds
            heartbeat_elapsed += POLL_INTERVAL
            if heartbeat_elapsed >= HEARTBEAT_INTERVAL:
                publish_etat()
                heartbeat_elapsed = 0

            # Sleep until next interval
            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    # Graceful shutdown handler
    def shutdown_handler(sig, frame):
        logger.info(f"Received signal {sig}. Shutting down...")
        disconnect_mqtt()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        asyncio.run(main())
    finally:
        disconnect_mqtt()
