from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, Integer, String, Float, DateTime
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
