"""Central configuration for the H2S AI Prediction Engine (Brick 4)."""
from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent          # Modele_AI/
RAW_ENERGY_FILES = [ROOT / f"energy_minute{i}.csv" for i in range(1, 5)]
RAW_METEO_FILE = ROOT / "open-meteo.csv"
EXTERNAL_DIR = ROOT / "data" / "external"               # cached API downloads
SCHEDULE_DIR = ROOT / "data" / "schedules"              # industrial 24h schedules
MODELS_DIR = ROOT / "trained_models"
REPORTS_DIR = ROOT / "reports"
FIGURES_DIR = REPORTS_DIR / "figures"
METRICS_FILE = MODELS_DIR / "metrics.json"

# ---------------------------------------------------------------------------
# Site
# ---------------------------------------------------------------------------
SITE_LAT = 48.90
SITE_LON = 2.35
PRICE_ZONE = "FR"

# Simulated renewable assets (kept identical to the v1 POC in model.py)
P_PV_W = 200.0            # PV peak power (W) – production = P_PV * GHI / 1000
P_WIND_W = 200.0          # Wind turbine rated power (W)
V_CUT_IN, V_NOM, V_CUT_OUT = 3.0, 12.0, 25.0   # m/s
BATTERY_CAPACITY_WH = 2200.0
BATTERY_SOC_INIT_WH = 600.0

# ---------------------------------------------------------------------------
# Time windows
# ---------------------------------------------------------------------------
# Long history used for exogenous models (weather / price / production)
HISTORY_START = "2024-01-01"
HISTORY_END = "2026-02-05"
EXO_VALID_START = "2025-10-01"   # 2024-01 -> 2025-09 : train
EXO_TEST_START = "2026-01-01"    # 2025-10 -> 2025-12 : validation ; 2026-01 -> 2026-02-05 : test

# Site measurement window (Shelly plugs) – consumption models: 70 / 15 / 15 chronological split
SPLIT_TRAIN, SPLIT_VALID = 0.70, 0.15

# ---------------------------------------------------------------------------
# Modelling
# ---------------------------------------------------------------------------
RANDOM_STATE = 42
N_CV_SPLITS = 5
N_OPTUNA_TRIALS = 30          # Bayesian optimisation (TPE) trials per model
EARLY_STOPPING_ROUNDS = 100

DEVICE_ON_THRESHOLD_W = 2.0   # a plug is considered "on" above this power
PEAK_QUANTILE = 0.90          # peaks = values above the P90 of the training distribution

# Confidence score levels (%), see h2s_forecast.confidence
CONFIDENCE_HIGH = 85.0
CONFIDENCE_MEDIUM = 70.0

MODEL_NAMES = [
    "production_1h", "production_24h",
    "consumption_1h", "consumption_24h_fallback",
    "weather_1h", "weather_24h",
    "price_1h", "price_24h",
]
