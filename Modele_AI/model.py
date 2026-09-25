import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

df1= pd.read_csv("energy_minute1.csv")
df2= pd.read_csv("energy_minute2.csv")
df3= pd.read_csv("energy_minute3.csv")
df4= pd.read_csv("energy_minute4.csv")

df1.head()
df2.head()
df3.head()
df4.head()
# Combiner tous les DataFrames
df = pd.concat([df1, df2, df3, df4], ignore_index=True)


# Remplacer les NaN par 0 ou une autre méthode
df.fillna(0, inplace=True)
df.shape
df.info()
# Convertir timestamp en datetime
df['timestamp'] = pd.to_datetime(df['timestamp'])

# Trier par timestamp
df = df.sort_values(['timestamp']).reset_index(drop=True)
df.head()
df.head(30)
# Créer une colonne "hour" qui ne garde que l'heure
df['hour'] = df['timestamp'].dt.floor('h')  # arrondi à l'heure
# Agréger par heure et appareil
df_hourly = df.groupby(['hour', 'device_id', 'device_name'])['energy_minute_wh'].sum().reset_index()

print(df_hourly)
df_hourly.head(15)
df_hourly.shape
df_hourly.tail()
# One-hot encoding des appareils
df_ohe = pd.get_dummies(
    df_hourly,
    columns=['device_name']
)

# Colonnes appareils (issues du one-hot)
device_cols = [
    col for col in df_ohe.columns
    if col.startswith('device_name_')
]

# Pondérer par l'énergie
for col in device_cols:
    df_ohe[col] = df_ohe[col] * df_ohe['energy_minute_wh']

# Agrégation par heure
df_hourly_devices = (
    df_ohe
    .groupby('hour')
    .agg(
        {**{col: 'sum' for col in device_cols},
         'energy_minute_wh': 'sum'}
    )
    .reset_index()
)

# Renommer les colonnes → enlever 'device_name_'
df_hourly_devices = df_hourly_devices.rename(
    columns=lambda c: c.replace('device_name_', '')
)

# Renommer l'énergie totale
df_hourly_devices = df_hourly_devices.rename(
    columns={'energy_minute_wh': 'energy_total_wh'}
)

df_hourly_devices

df_hourly_devices = df_hourly_devices.rename(columns={'hour': 'time'})
df_hourly_devices.shape
df_meteo = pd.read_csv("open-meteo.csv")

df_meteo['time'] = pd.to_datetime(df_meteo['time'])

df_meteo.head()
df_meteo.shape
df_meteo['time'] = df_meteo['time'].dt.floor('h')
df_hourly_devices['time'] = df_hourly_devices['time'].dt.floor('h')
df_final = pd.merge(
    df_hourly_devices,
    df_meteo,
    on='time',
    how='inner'
)
df_final.shape
df_final.head()
P_PV = 200  # W

df_final['solar_wh'] = (
    P_PV * df_final['radiation'] / 1000
).clip(lower=0)
P_WIND = 200      # W
V_NOM = 12        # m/s
V_CUT_IN = 3      # m/s
V_CUT_OUT = 25    # m/s

def wind_power(v):
    if v < V_CUT_IN or v > V_CUT_OUT:
        return 0
    elif v <= V_NOM:
        return P_WIND * (v / V_NOM) ** 3
    else:
        return P_WIND

df_final['wind_wh'] = df_final['wind'].apply(wind_power)

df_final['production_wh'] = (
    df_final['solar_wh'] + df_final['wind_wh']
)
df_final['energy_balance_wh'] = (
    df_final['production_wh'] - df_final['energy_total_wh']
)
# Paramètres batterie
BATTERY_CAPACITY = 2200  # Wh
battery_soc = 600        # SOC initial réel

# Initialiser colonnes (vides, la boucle remplira)
df_final['battery_soc'] = 0.0
df_final['grid_needed'] = 0.0
df_final['battery_charge'] = 0.0
df_final['battery_discharge'] = 0.0

for i, row in df_final.iterrows():
    renewable = row['production_wh']
    consumption = row['energy_total_wh']

    net = renewable - consumption

    if net >= 0:
        # Charge batterie
        charge_possible = min(net, BATTERY_CAPACITY - battery_soc)
        battery_soc += charge_possible
        battery_charge = charge_possible
        battery_discharge = 0
        grid_draw = 0
    else:
        # Décharge batterie
        discharge_possible = min(-net, battery_soc)
        battery_soc -= discharge_possible
        battery_discharge = discharge_possible
        battery_charge = 0
        grid_draw = -net - discharge_possible

    # Sécurité physique
    battery_soc = max(0, min(battery_soc, BATTERY_CAPACITY))

    # Enregistrement
    df_final.at[i, 'battery_soc'] = battery_soc
    df_final.at[i, 'battery_charge'] = battery_charge
    df_final.at[i, 'battery_discharge'] = battery_discharge
    df_final.at[i, 'grid_needed'] = grid_draw

import matplotlib.pyplot as plt
import matplotlib.pyplot as plt
import seaborn as sns

sns.set(style="whitegrid")

fig, ax1 = plt.subplots(figsize=(16,6))

# Couleurs
color_consumption = '#FF7F0E'
color_production = '#2CA02C'
color_battery = '#1F77B4'

# 🔹 Axe principal : consommation & production
ax1.bar(df_final['time'], df_final['energy_total_wh'],
        color=color_consumption, alpha=0.6, width=0.03,
        label='Consommation totale (Wh)')

ax1.plot(df_final['time'], df_final['production_wh'],
         color=color_production, marker='o',
         linewidth=2, markersize=6,
         label='Production renouvelable (Wh)')

ax1.set_xlabel("Heure")
ax1.set_ylabel("Énergie (Wh)")

# 🔹 Axe secondaire : batterie
ax2 = ax1.twinx()
ax2.plot(df_final['time'], df_final['battery_soc'],
         color=color_battery, marker='s',
         linewidth=2, markersize=6,
         label='Batterie SOC (Wh)')

ax2.set_ylabel("État de charge batterie (Wh)")
ax2.set_ylim(0, BATTERY_CAPACITY)

# 🔹 Titre
plt.title("Simulation énergétique horaire", fontsize=16, fontweight='bold')

# 🔹 Légende combinée
lines1, labels1 = ax1.get_legend_handles_labels()
lines2, labels2 = ax2.get_legend_handles_labels()
ax1.legend(lines1 + lines2, labels1 + labels2, loc='upper left')

plt.xticks(rotation=45, ha='right')
plt.tight_layout()
plt.show()


df_final.head()
df_final.isna().sum()
df_final['time'] = pd.to_datetime(df_final['time'])

df_final = df_final.sort_values('time')


df_final.to_csv('df_final.csv', index=False)
df_final[['Cafetiere','Ecran','Lampe Chambre','Lampe Cuisine','Lampe Salle à manger',
          'PC Portable','TV Salle à manger','Telephone / Tablette']].mean()

df_final[['solar_wh','wind_wh','production_wh']].mean()
df_final[['battery_soc','battery_charge','battery_discharge']].describe()

df_final['grid_needed'].sum()

df_final.head()
# =============================================================================
# LIGHTGBM MULTI-OUTPUT – ENERGY PREDICTION (PRODUCTION READY)
# =============================================================================

# =============================================================================
# 1. IMPORTS
# =============================================================================
import pandas as pd
import numpy as np
import lightgbm as lgb
import optuna
import os
import joblib
from sklearn.model_selection import TimeSeriesSplit
from sklearn.multioutput import MultiOutputRegressor
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score
import matplotlib.pyplot as plt
import seaborn as sns
import warnings
warnings.filterwarnings("ignore")

# =============================================================================
# 2. CONFIGURATION
# =============================================================================
RANDOM_STATE = 42
N_TRIALS = 50

TARGET_COLUMNS = [
    "energy_total_wh",
    "production_wh",
    "battery_soc"
]

# We will predict T+1h and T+24h
TARGETS_1H = [f"{t}_target_1h" for t in TARGET_COLUMNS]
TARGETS_24H = [f"{t}_target_24h" for t in TARGET_COLUMNS]

# Data to drop to prevent leakage
# Device columns contain current consumption data which sums to energy_total_wh
DEVICE_COLS = ["Lampe Chambre", "Lampe Cuisine", "Lampe Salle à manger",
               "Ecran", "PC Portable", "TV Salle à manger", "Telephone / Tablette", "Cafetiere"]

LEAKAGE_COLS = [
    "energy_balance_wh",
    "battery_charge",
    "battery_discharge",
    "grid_needed"
] + DEVICE_COLS + TARGET_COLUMNS # Drop current targets too because they are basically the device cols and other targets

# =============================================================================
# 3. FEATURE ENGINEERING (CONTEXTUAL & HISTORICAL)
# =============================================================================
def prepare_features(df):
    df = df.copy()

    # Ensure 'time' is datetime and sort
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"])
        df = df.sort_values("time").reset_index(drop=True)

    # -------------------------------------------------------------------------
    # CONTEXTUAL FEATURES (Time, Weather)
    # -------------------------------------------------------------------------
    if "time" in df.columns:
        df["hour"] = df["time"].dt.hour
        df["dayofweek"] = df["time"].dt.dayofweek
        df["month"] = df["time"].dt.month
        df["is_weekend"] = (df["dayofweek"] >= 5).astype(int)

        df["hour_sin"] = np.sin(2 * np.pi * df["hour"] / 24)
        df["hour_cos"] = np.cos(2 * np.pi * df["hour"] / 24)
        df["month_sin"] = np.sin(2 * np.pi * df["month"] / 12)
        df["month_cos"] = np.cos(2 * np.pi * df["month"] / 12)

    if "solar_wh" in df.columns and "radiation" in df.columns:
        df["solar_efficiency"] = df["solar_wh"] / (df["radiation"].replace(0, 1e-6))
        df["is_night"] = (df["radiation"] < 1).astype(int)
    elif "radiation" in df.columns:
        df["is_night"] = (df["radiation"] < 1).astype(int)
        df["solar_wh"] = df.get("solar_wh", 0)
        df["solar_efficiency"] = df["solar_wh"] / (df["radiation"].replace(0, 1e-6))
    else:
        df["solar_efficiency"] = 0
        df["is_night"] = 0

    # -------------------------------------------------------------------------
    # HISTORICAL FEATURES (Lags & Moving Averages)
    # -------------------------------------------------------------------------
    for lag in [1, 2, 24]:
        for col in TARGET_COLUMNS:
            if col in df.columns:
                df[f"{col}_lag{lag}"] = df[col].shift(lag)
                
    for window in [6, 24]:
        for col in TARGET_COLUMNS:
            if col in df.columns:
                df[f"{col}_rolling_mean_{window}h"] = df[col].shift(1).rolling(window=window).mean()

    # -------------------------------------------------------------------------
    # TARGET SHIFTING (Future Prediction)
    # -------------------------------------------------------------------------
    for target in TARGET_COLUMNS:
        df[f"{target}_target_1h"] = df[target].shift(-1)
        df[f"{target}_target_24h"] = df[target].shift(-24)

    # -------------------------------------------------------------------------
    # CLEANUP
    # -------------------------------------------------------------------------
    # Drop rows with NaN (due to lags and rolling windows)
    df = df.dropna().reset_index(drop=True)
    return df

# =============================================================================
# 4. PREPARE X & Y
# =============================================================================
def get_X_y(df, target_horizon="1h"):
    df_feat = prepare_features(df)
    
    # Select targets based on horizon
    if target_horizon == "1h":
        y_cols = TARGETS_1H
        drop_targets = TARGETS_24H
    elif target_horizon == "24h":
        y_cols = TARGETS_24H
        drop_targets = TARGETS_1H
    else:
        raise ValueError("target_horizon must be '1h' or '24h'")
        
    y = df_feat[y_cols]
    
    # X excludes future targets and leakage columns
    X = df_feat.drop(columns=y_cols + drop_targets)
    X = X.drop(columns=[c for c in LEAKAGE_COLS if c in X.columns])
    
    # Keep time for plotting but don't use it as feature
    time_col = X.pop("time") if "time" in X.columns else None
    
    return X, y, time_col

# =============================================================================
# 5. OBJECTIVE FUNCTION FOR OPTUNA (With TimeSeriesSplit)
# =============================================================================
def objective_cv(trial, X, y, n_splits=5):
    params = {
        "n_estimators": trial.suggest_int("n_estimators", 200, 800),
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
        "num_leaves": trial.suggest_int("num_leaves", 32, 128),
        "max_depth": trial.suggest_int("max_depth", 4, 10),
        "min_child_samples": trial.suggest_int("min_child_samples", 10, 80),
        "subsample": trial.suggest_float("subsample", 0.7, 1.0),
        "colsample_bytree": trial.suggest_float("colsample_bytree", 0.7, 1.0),
        "reg_alpha": trial.suggest_float("reg_alpha", 1e-6, 5.0, log=True),
        "reg_lambda": trial.suggest_float("reg_lambda", 1e-6, 5.0, log=True),
        "random_state": RANDOM_STATE,
        "verbosity": -1
    }

    # TEMPORAL CROSS-VALIDATION
    tscv = TimeSeriesSplit(n_splits=n_splits)
    rmses = []

    for train_idx, valid_idx in tscv.split(X):
        X_train, X_valid = X.iloc[train_idx], X.iloc[valid_idx]
        y_train, y_valid = y.iloc[train_idx], y.iloc[valid_idx]

        model = MultiOutputRegressor(lgb.LGBMRegressor(**params))
        model.fit(X_train, y_train)
        preds = model.predict(X_valid)

        fold_rmses = [
            np.sqrt(mean_squared_error(y_valid.iloc[:, i], preds[:, i]))
            for i in range(y_valid.shape[1])
        ]
        rmses.append(np.mean(fold_rmses))

    return np.mean(rmses)

# =============================================================================
# 6. TRAINING PIPELINE
# =============================================================================
def train_model(df, target_horizon="1h", use_optuna=True):
    print(f"\n--- Training Model for Horizon: {target_horizon} ---")
    X, y, time_col = get_X_y(df, target_horizon=target_horizon)

    # Time series split: 80% train, 20% test
    split_idx = int(len(X) * 0.8)
    X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]
    time_test = time_col.iloc[split_idx:] if time_col is not None else None

    # Hyperparameter tuning
    if use_optuna:
        study = optuna.create_study(direction="minimize")
        study.optimize(lambda trial: objective_cv(trial, X_train, y_train, n_splits=5), n_trials=N_TRIALS)
        best_params = study.best_params
        print("Best params:", best_params)
    else:
        # Fallback to sensible defaults
        best_params = {
            'n_estimators': 300, 'learning_rate': 0.05, 'num_leaves': 64,
            'max_depth': 6, 'min_child_samples': 20, 'subsample': 0.8,
            'colsample_bytree': 0.8, 'random_state': RANDOM_STATE, 'verbosity': -1
        }
        print("Using default params.")

    # Train final model on full training set
    model = MultiOutputRegressor(lgb.LGBMRegressor(**best_params))
    model.fit(X_train, y_train)

    # Evaluate on test set
    preds = model.predict(X_test)
    print("\n================= TEST METRICS ================")
    for i, target in enumerate(y.columns):
        rmse = np.sqrt(mean_squared_error(y_test.iloc[:, i], preds[:, i]))
        mae = mean_absolute_error(y_test.iloc[:, i], preds[:, i])
        r2 = r2_score(y_test.iloc[:, i], preds[:, i])
        print(f"\nTarget: {target}")
        print(f"RMSE: {rmse:.2f}")
        print(f"MAE : {mae:.2f}")
        print(f"R²  : {r2:.3f}")

    return model, X_test, y_test, time_test, preds

# =============================================================================
# 7. EXECUTE PIPELINE
# =============================================================================
if 'df_final' in locals() or 'df_final' in globals():
    # Model for 1 Hour Ahead
    model_1h, X_test_1h, y_test_1h, time_test_1h, preds_1h = train_model(df_final, target_horizon="1h", use_optuna=False)
    
    # Model for 24 Hours Ahead
    model_24h, X_test_24h, y_test_24h, time_test_24h, preds_24h = train_model(df_final, target_horizon="24h", use_optuna=False)

    # =============================================================================
    # 8. VISUALIZATION (ENHANCED)
    # =============================================================================
    sns.set_theme(style="darkgrid", context="talk")
    
    def plot_results(y_test, preds, time_test, horizon):
        os.makedirs("trained_models", exist_ok=True)
        fig, axes = plt.subplots(3, 1, figsize=(18, 15), sharex=True)
        colors = ['#3498db', '#e74c3c', '#2ecc71']
        targets = y_test.columns
        
        for i, target in enumerate(targets):
            ax = axes[i]
            ax.plot(time_test, y_test.iloc[:, i], label=f'Actual', color=colors[i], linestyle='-', linewidth=2, alpha=0.8)
            ax.plot(time_test, preds[:, i], label=f'Predicted', color='black', linestyle='--', linewidth=2, alpha=0.7)
            
            # Fill between to show errors visually
            ax.fill_between(time_test, y_test.iloc[:, i], preds[:, i], color='gray', alpha=0.2)
            
            ax.set_title(f'{target.replace("_", " ").title()} ({horizon} Ahead)', fontsize=16, fontweight='bold', loc='left')
            ax.set_ylabel('Value', fontsize=12)
            ax.legend(loc='upper left')
            
        axes[-1].set_xlabel('Time', fontsize=14)
        plt.tight_layout()
        plt.savefig(f'trained_models/predictions_plot_{horizon}.png', dpi=300, bbox_inches='tight')
        print(f"Saved visualization for {horizon} to trained_models/predictions_plot_{horizon}.png")
        plt.close(fig)

    print("\nGenerating and saving visualizations...")
    plot_results(y_test_1h, preds_1h, time_test_1h, "1H")
    plot_results(y_test_24h, preds_24h, time_test_24h, "24H")
    
    # =============================================================================
    # 9. SAVE MODELS
    # =============================================================================
    os.makedirs("trained_models", exist_ok=True)
    joblib.dump(model_1h, 'trained_models/lgbm_model_1h.joblib')
    joblib.dump(model_24h, 'trained_models/lgbm_model_24h.joblib')
    print("\nModels successfully saved to the 'trained_models' directory.")

