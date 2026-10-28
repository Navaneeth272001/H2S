from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean
from datetime import datetime
from .config import DATABASE_URL

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
Base = declarative_base()

class EnergyMinute(Base):
    __tablename__ = "energy_minute"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    device_id = Column(Integer, index=True)
    device_name = Column(String)
    current_a = Column(Float, nullable=True)
    voltage_v = Column(Float, nullable=True)
    power_w = Column(Float, nullable=True)
    energy_minute_wh = Column(Float, nullable=True)
    relay_state = Column(Boolean, nullable=True)
    measurement_quality = Column(String, default="UNAVAILABLE")
    measurement_timestamp = Column(DateTime(timezone=True), nullable=True)
    energy_total_wh = Column(Float, nullable=True)
    shelly_model = Column(String, nullable=True)
    shelly_firmware = Column(String, nullable=True)


class ShellyCommand(Base):
    __tablename__ = "shelly_command"
    id = Column(Integer, primary_key=True, index=True)
    correlation_id = Column(String, index=True, nullable=True)
    action = Column(String, nullable=False)
    execution_mode = Column(String, nullable=False)
    status = Column(String, nullable=False)
    device_id = Column(Integer, nullable=True, index=True)
    device_name = Column(String, nullable=True)
    switch_id = Column(Integer, nullable=True)
    target_state = Column(Boolean, nullable=True)
    command_sent = Column(Boolean, nullable=True)
    command_acknowledged = Column(Boolean, nullable=True)
    state_before = Column(Boolean, nullable=True)
    state_after = Column(Boolean, nullable=True)
    verified = Column(Boolean, nullable=True)
    measurement_before_w = Column(Float, nullable=True)
    measurement_after_w = Column(Float, nullable=True)
    measurement_quality = Column(String, nullable=True)
    latency_ms = Column(Float, nullable=True)
    error = Column(String, nullable=True)
    result_json = Column(String, nullable=True)
    issued_at = Column(DateTime(timezone=True), nullable=True)
    observed_at = Column(DateTime(timezone=True), nullable=True)

class BatteryMinute(Base):
    __tablename__ = "battery_minute"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    stock_id = Column(Integer, index=True)
    name = Column(String)
    soc_pct = Column(Float)
    power_charge_w = Column(Float)
    power_discharge_w = Column(Float)
    voltage_v = Column(Float)
    current_a = Column(Float)
    temperature_c = Column(Float)
    is_simulated = Column(Boolean, default=True)
    action = Column(String, default="STANDBY")
    simulated_production_w = Column(Float, nullable=True)
    simulated_surplus_w = Column(Float, nullable=True)

class ProductionMinute(Base):
    """Simulated renewable production, one row per source per minute.

    ``energy_total_wh`` is the cumulative counter republished on the device status topic: it
    is read back at restart so the index the agent differentiates never walks backwards.
    """

    __tablename__ = "production_minute"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    device_id = Column(Integer, index=True)
    name = Column(String)
    source_type = Column(String)
    power_w = Column(Float)
    voltage_v = Column(Float)
    current_a = Column(Float)
    energy_total_wh = Column(Float)
    irradiance_w_m2 = Column(Float, nullable=True)
    wind_speed_m_s = Column(Float, nullable=True)


class EnergyHourly(Base):
    __tablename__ = "energy_hourly"
    id = Column(Integer, primary_key=True, index=True)
    start_time = Column(DateTime, index=True)
    end_time = Column(DateTime, index=True)
    device_id = Column(Integer, index=True)
    device_name = Column(String)
    avg_current_a = Column(Float)
    avg_voltage_v = Column(Float)
    avg_power_w = Column(Float)
    total_energy_wh = Column(Float)

class WeatherHourly(Base):
    __tablename__ = "weather_hourly"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, index=True)
    temperature = Column(Float)
    humidity = Column(Float)
    wind_speed = Column(Float)
    precipitation = Column(Float)
    condition = Column(String)
    ghi = Column(Float)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Upgrade existing collector databases without dropping user data.
        await conn.exec_driver_sql("ALTER TABLE energy_minute ADD COLUMN IF NOT EXISTS relay_state BOOLEAN")
        await conn.exec_driver_sql("ALTER TABLE energy_minute ADD COLUMN IF NOT EXISTS measurement_quality VARCHAR DEFAULT 'UNAVAILABLE'")
        await conn.exec_driver_sql("ALTER TABLE energy_minute ADD COLUMN IF NOT EXISTS measurement_timestamp TIMESTAMPTZ")
        await conn.exec_driver_sql("ALTER TABLE energy_minute ADD COLUMN IF NOT EXISTS energy_total_wh FLOAT")
        await conn.exec_driver_sql("ALTER TABLE energy_minute ADD COLUMN IF NOT EXISTS shelly_model VARCHAR")
        await conn.exec_driver_sql("ALTER TABLE energy_minute ADD COLUMN IF NOT EXISTS shelly_firmware VARCHAR")
        await conn.exec_driver_sql("ALTER TABLE shelly_command ADD COLUMN IF NOT EXISTS result_json VARCHAR")
        await conn.exec_driver_sql("ALTER TABLE battery_minute ADD COLUMN IF NOT EXISTS simulated_production_w FLOAT")
        await conn.exec_driver_sql("ALTER TABLE battery_minute ADD COLUMN IF NOT EXISTS simulated_surplus_w FLOAT")
        await conn.exec_driver_sql("ALTER TABLE production_minute ADD COLUMN IF NOT EXISTS source_type VARCHAR")
        await conn.exec_driver_sql("ALTER TABLE production_minute ADD COLUMN IF NOT EXISTS irradiance_w_m2 FLOAT")
        await conn.exec_driver_sql("ALTER TABLE production_minute ADD COLUMN IF NOT EXISTS wind_speed_m_s FLOAT")
