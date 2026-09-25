import asyncio
import aiohttp
import logging
import signal
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from src.config import SHELLY_DEVICES, POLL_INTERVAL, MINUTES_PER_HOUR, TIMEZONE, MAX_ERRORS
from src.database import init_db, AsyncSessionLocal, EnergyMinute, EnergyHourly, WeatherHourly
from src.collector import fetch_shelly, fetch_weather
from src.notifier import send_email
from src.mqtt_publisher import (
    publish_energy_minute, publish_energy_hourly,
    publish_weather_hourly, disconnect_mqtt
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("main_collector")

async def main():
    logger.info("Starting H2S Shelly & Weather Collector...")
    await init_db()

    error_counts = {dev_id: 0 for dev_id in SHELLY_DEVICES}
    email_sent = {dev_id: False for dev_id in SHELLY_DEVICES}
    
    # buffers for hourly aggregation
    buffers = {dev_id: {"energy": 0.0, "p": 0.0, "i": 0.0, "v": 0.0, "c": 0} for dev_id in SHELLY_DEVICES}
    minute_count = 0
    
    tz = ZoneInfo(TIMEZONE)
    hour_start = datetime.now(tz).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)

    async with aiohttp.ClientSession() as session:
        while True:
            now = datetime.now(tz)
            
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
                        energy_minute_wh = round(power / 60, 3)

                        # Insert into minute table
                        em = EnergyMinute(
                            timestamp=now.replace(tzinfo=None),
                            device_id=dev_id,
                            device_name=res["device_name"],
                            current_a=current,
                            voltage_v=voltage,
                            power_w=power,
                            energy_minute_wh=energy_minute_wh
                        )
                        db_session.add(em)

                        # Publish to MQTT (cloud server)
                        publish_energy_minute(
                            device_id=dev_id,
                            device_name=res["device_name"],
                            current_a=current,
                            voltage_v=voltage,
                            power_w=power,
                            energy_minute_wh=energy_minute_wh,
                            timestamp=now.replace(tzinfo=None)
                        )
                        
                        # Accumulate in buffer
                        buffers[dev_id]["energy"] += energy_minute_wh
                        buffers[dev_id]["p"] += power
                        buffers[dev_id]["i"] += current
                        buffers[dev_id]["v"] += voltage
                        buffers[dev_id]["c"] += 1
                        
                    else:
                        error_counts[dev_id] += 1
                        if error_counts[dev_id] >= MAX_ERRORS and not email_sent[dev_id]:
                            await send_email(
                                subject=f"🚨 Shelly Offline: {res['device_name']}",
                                message=f"Device ID: {dev_id} is unreachable."
                            )
                            email_sent[dev_id] = True
                            
                await db_session.commit()
                
            logger.info(f"Minute data saved for {len(results)} devices.")
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
                            timestamp=hour_start.replace(tzinfo=None)
                        )
                    
                    # Save Hourly Energy
                    for dev_id, b in buffers.items():
                        if b["c"] > 0:
                            eh = EnergyHourly(
                                start_time=hour_start.replace(tzinfo=None),
                                end_time=hour_end.replace(tzinfo=None),
                                device_id=dev_id,
                                device_name=SHELLY_DEVICES[dev_id]["name"],
                                avg_current_a=round(b["i"] / b["c"], 3),
                                avg_voltage_v=round(b["v"] / b["c"], 1),
                                avg_power_w=round(b["p"] / b["c"], 1),
                                total_energy_wh=round(b["energy"], 2)
                            )
                            db_session.add(eh)

                            # Publish hourly energy to MQTT
                            publish_energy_hourly(
                                device_id=dev_id,
                                device_name=SHELLY_DEVICES[dev_id]["name"],
                                avg_current_a=round(b["i"] / b["c"], 3),
                                avg_voltage_v=round(b["v"] / b["c"], 1),
                                avg_power_w=round(b["p"] / b["c"], 1),
                                total_energy_wh=round(b["energy"], 2),
                                start_time=hour_start.replace(tzinfo=None),
                                end_time=hour_end.replace(tzinfo=None)
                            )
                            
                    await db_session.commit()
                    logger.info("Hourly data committed.")
                
                # Reset
                buffers = {d: {"energy": 0.0, "p": 0.0, "i": 0.0, "v": 0.0, "c": 0} for d in SHELLY_DEVICES}
                minute_count = 0
                hour_start = hour_end

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
