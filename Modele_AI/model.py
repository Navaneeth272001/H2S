import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from google.colab import drive
drive.mount('/content/drive')
df1= pd.read_csv("/content/drive/MyDrive/model2/energy_minute1.csv")
df2=pd.read_csv("/content/drive/MyDrive/model2/energy_minute2.csv")
df3= pd.read_csv("/content/drive/MyDrive/model2/energy_minute3.csv")
df4=pd.read_csv("/content/drive/MyDrive/model2/energy_minute4.csv")

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
df['hour'] = df['timestamp'].dt.floor('H')  # arrondi à l'heure
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
df_meteo = pd.read_csv("/content/drive/MyDrive/model2/open-meteo.csv")

df_meteo['time'] = pd.to_datetime(df_meteo['time'])

df_meteo.head()
df_meteo.shape
df_meteo['time'] = df_meteo['time'].dt.floor('H')
df_hourly_devices['time'] = df_hourly_devices['time'].dt.floor('H')
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
df_final['battery_soc'] = 0
df_final['grid_needed'] = 0
df_final['battery_charge'] = 0
df_final['battery_discharge'] = 0

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
# Targets:
# - energy_total_wh
# - production_wh
# - battery_soc
# =============================================================================

# =============================================================================
# 1. INSTALLATION
# =============================================================================
!pip install lightgbm optuna scikit-learn matplotlib seaborn -q

# =============================================================================
# 2. IMPORTS
# =============================================================================
import pandas as pd
import numpy as np
import lightgbm as lgb
import optuna

from sklearn.model_selection import train_test_split
from sklearn.multioutput import MultiOutputRegressor
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score

import matplotlib.pyplot as plt
import seaborn as sns
import warnings
warnings.filterwarnings("ignore")

# =============================================================================
# 3. CONFIGURATION
# =============================================================================
RANDOM_STATE = 42
N_TRIALS = 50

TARGET_COLUMNS = [
    "energy_total_wh",
    "production_wh",
    "battery_soc"
]

LEAKAGE_COLS = [
    "energy_balance_wh",
    "battery_charge",
    "battery_discharge",
    "grid_needed"
]

# =============================================================================
# 4. FEATURE ENGINEERING (INDUSTRY GRADE)
# =============================================================================
def prepare_features(df, drop_time=True, drop_na=True, include_lag_targets=True):
    df = df.copy()

    # Ensure 'time' is datetime and sort for correct lag creation
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"])
        df = df.sort_values("time").reset_index(drop=True)

    # -------------------------------------------------------------------------
    # TIME FEATURES
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

    # -------------------------------------------------------------------------
    # LOAD AGGREGATES (CRITICAL)
    # -------------------------------------------------------------------------
    # Check if device columns exist before creating aggregates
    device_cols = ["Lampe Chambre", "Lampe Cuisine", "Lampe Salle à manger",
                   "Ecran", "PC Portable", "TV Salle à manger", "Telephone / Tablette", "Cafetiere"]
    if all(col in df.columns for col in device_cols):
        df["total_lighting_wh"] = (
            df["Lampe Chambre"] +
            df["Lampe Cuisine"] +
            df["Lampe Salle à manger"]
        )

        df["total_plug_wh"] = (
            df["Ecran"] +
            df["PC Portable"] +
            df["TV Salle à manger"] +
            df["Telephone / Tablette"]
        )

        df["total_load_wh"] = (
            df["total_lighting_wh"] +
            df["total_plug_wh"] +
            df["Cafetiere"]
        )
    else:
        # Create dummy columns if missing to prevent errors, or handle as appropriate for your data.
        # For df_final, these columns should exist.
        for col in device_cols:
            if col not in df.columns:
                df[col] = 0.0 # Or raise an error if these are critical

    # -------------------------------------------------------------------------
    # SOLAR / WEATHER FEATURES
    # -------------------------------------------------------------------------
    if "solar_wh" in df.columns and "radiation" in df.columns:
        # Avoid division by zero if radiation is 0
        df["solar_efficiency"] = df["solar_wh"] / (df["radiation"].replace(0, 1e-6))
        df["is_night"] = (df["radiation"] < 1).astype(int)
    elif "radiation" in df.columns:
        df["is_night"] = (df["radiation"] < 1).astype(int)
        if "solar_wh" not in df.columns:
             df["solar_wh"] = 0 # Dummy if missing
        df["solar_efficiency"] = df["solar_wh"] / (df["radiation"].replace(0, 1e-6))
    else:
        df["solar_efficiency"] = 0
        df["is_night"] = 0


    # -------------------------------------------------------------------------
    # LAG FEATURES (TIME SERIES – VERY IMPORTANT) for current timestep's X
    # -------------------------------------------------------------------------
    if include_lag_targets:
        for lag in [1, 2, 3]:
            for target_col in TARGET_COLUMNS:
                if target_col in df.columns: # Ensure the column exists before creating a lag
                    df[f"{target_col}_lag{lag}"] = df[target_col].shift(lag)

    # -------------------------------------------------------------------------
    # REMOVE TIME COLUMN (conditional)
    # -------------------------------------------------------------------------
    if drop_time and "time" in df.columns:
        df = df.drop(columns=["time"])

    # -------------------------------------------------------------------------
    # DROP NA (conditional)
    # -------------------------------------------------------------------------
    if drop_na:
        df = df.dropna()

    return df
import pandas as pd
import numpy as np
import lightgbm as lgb
import optuna

from sklearn.model_selection import train_test_split
from sklearn.multioutput import MultiOutputRegressor
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score

import matplotlib.pyplot as plt
import seaborn as sns
import warnings
warnings.filterwarnings("ignore")

from sklearn.model_selection import KFold

def objective_cv(trial, X, y, n_splits=5):
    # Suggest hyperparameters
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

    kf = KFold(n_splits=n_splits, shuffle=True, random_state=RANDOM_STATE)
    rmses = []

    for train_idx, valid_idx in kf.split(X):
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

    # Return mean RMSE across folds
    return np.mean(rmses)
import optuna

study = optuna.create_study(direction="minimize")
study.optimize(lambda trial: objective_cv(trial, X, y, n_splits=5), n_trials=50)

print("Best params:", study.best_params)
print("Best RMSE:", study.best_value)
import pandas as pd
import numpy as np
import lightgbm as lgb
from sklearn.multioutput import MultiOutputRegressor
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score
from sklearn.model_selection import KFold
import warnings
warnings.filterwarnings("ignore")

# ================================
# Settings
# ================================
RANDOM_STATE = 42
TARGET_COLUMNS = [
    "energy_total_wh",
    "production_wh",
    "battery_soc"
]  # Corrected target outputs
LEAKAGE_COLS = [
    "energy_balance_wh",
    "battery_charge",
    "battery_discharge",
    "grid_needed"
]  # Corrected leakage columns

# ================================
# Best parameters (from previous Optuna run)
# ================================
best_params = {
    'n_estimators': 272,
    'learning_rate': 0.09554820977656957,
    'num_leaves': 98,
    'max_depth': 9,
    'min_child_samples': 10,
    'subsample': 0.7487107797000587,
    'colsample_bytree': 0.8785698577169511,
    'reg_alpha': 1.0479049013702662,
    'reg_lambda': 4.2651377222939795,
    'random_state': RANDOM_STATE,
    'verbosity': -1
}

# ================================
# Prepare features and targets
# ================================
def get_X_y(df):
    df = prepare_features(df)  # your custom feature engineering
    X = df.drop(columns=TARGET_COLUMNS)
    y = df[TARGET_COLUMNS]
    X = X.drop(columns=[c for c in LEAKAGE_COLS if c in X.columns])
    return X, y

# ================================
# Load dataset
# ================================
X, y = get_X_y(df_final)

# ================================
# Train final model on full dataset
# ================================
final_model = MultiOutputRegressor(lgb.LGBMRegressor(**best_params))
final_model.fit(X, y)
print("✅ Final model trained on full dataset with 3 outputs.")

# ================================
# Evaluate with cross-validation
# ================================
kf = KFold(n_splits=5, shuffle=True, random_state=RANDOM_STATE)
cv_r2 = []
cv_rmse = []

for train_idx, valid_idx in kf.split(X):
    X_train, X_valid = X.iloc[train_idx], X.iloc[valid_idx]
    y_train, y_valid = y.iloc[train_idx], y.iloc[valid_idx]

    model = MultiOutputRegressor(lgb.LGBMRegressor(**best_params))
    model.fit(X_train, y_train)
    preds = model.predict(X_valid)

    fold_rmse = [np.sqrt(mean_squared_error(y_valid.iloc[:, i], preds[:, i])) for i in range(y.shape[1])]
    fold_r2 = [r2_score(y_valid.iloc[:, i], preds[:, i]) for i in range(y.shape[1])]

    cv_rmse.append(np.mean(fold_rmse))
    cv_r2.append(np.mean(fold_r2))

print(f"\nCross-validated RMSE: {np.mean(cv_rmse):.4f}")
print(f"Cross-validated R² : {np.mean(cv_r2):.4f}")

# ================================
# Predict on new data
# ================================
# X_new = prepare_features(new_df)
# predictions = final_model.predict(X_new)
# pred_df = pd.DataFrame(predictions, columns=TARGET_COLUMNS)
# print(pred_df.head())

import pandas as pd
import numpy as np
import lightgbm as lgb
import optuna
from sklearn.multioutput import MultiOutputRegressor
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score
from sklearn.model_selection import train_test_split, KFold
import warnings

warnings.filterwarnings("ignore")

# ================================
# 1️⃣ Settings
# ================================
RANDOM_STATE = 42
N_TRIALS = 50  # number of Optuna trials
TARGET_COLUMNS = [
    "energy_total_wh",
    "production_wh",
    "battery_soc"
]
LEAKAGE_COLS = [
    "energy_balance_wh",
    "battery_charge",
    "battery_discharge",
    "grid_needed"
]

# ================================
# 2️⃣ Best parameters (from previous Optuna run)
# ================================
best_params = {
    'n_estimators': 272,
    'learning_rate': 0.09554820977656957,
    'num_leaves': 98,
    'max_depth': 9,
    'min_child_samples': 10,
    'subsample': 0.7487107797000587,
    'colsample_bytree': 0.8785698577169511,
    'reg_alpha': 1.0479049013702662,
    'reg_lambda': 4.2651377222939795,
    'random_state': RANDOM_STATE,
    'verbosity': -1
}

# ================================
# 3️⃣ Prepare features and targets
# ================================
def get_X_y(df):
    df = prepare_features(df)  # your custom feature engineering
    X = df.drop(columns=TARGET_COLUMNS)
    y = df[TARGET_COLUMNS]
    X = X.drop(columns=[c for c in LEAKAGE_COLS if c in X.columns])
    return X, y

# ================================
# 4️⃣ Objective function for Optuna
# ================================
def objective(trial, X_train, y_train, X_valid, y_valid):
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
    model = MultiOutputRegressor(lgb.LGBMRegressor(**params))
    model.fit(X_train, y_train)
    preds = model.predict(X_valid)

    # RMSE per target, then mean
    rmses = [np.sqrt(mean_squared_error(y_valid.iloc[:, i], preds[:, i])) for i in range(y_valid.shape[1])]
    return np.mean(rmses)

# ================================
# 5️⃣ Training pipeline
# ================================
def train_model(df, use_optuna=True):
    X, y = get_X_y(df)

    # --- Time series split: 80% train, 10% validation, 10% test ---
    X_trainval, X_test, y_trainval, y_test = train_test_split(
        X, y, test_size=0.10, shuffle=False
    )
    X_train, X_valid, y_train, y_valid = train_test_split(
        X_trainval, y_trainval, test_size=0.1111, shuffle=False
    )

    # --- Hyperparameter tuning ---
    if use_optuna:
        study = optuna.create_study(direction="minimize")
        study.optimize(
            lambda t: objective(t, X_train, y_train, X_valid, y_valid),
            n_trials=N_TRIALS
        )
        final_params = study.best_params
        print("\n✅ Best parameters from Optuna:")
        for k, v in final_params.items():
            print(f"{k}: {v}")
    else:
        final_params = best_params
        study = None
        print("\n✅ Using predefined best parameters.")

    # --- Train final model on train + valid ---
    # 'verbosity' is already in best_params, so remove explicit verbosity=-1
    final_model = MultiOutputRegressor(
        lgb.LGBMRegressor(**final_params)
    )
    X_full = pd.concat([X_train, X_valid])
    y_full = pd.concat([y_train, y_valid])
    final_model.fit(X_full, y_full)
    print("\n✅ Final model trained on full training + validation set.")

    # --- Evaluate on test set ---
    preds = final_model.predict(X_test)
    print("\n================= TEST METRICS ================")
    for i, target in enumerate(TARGET_COLUMNS):
        rmse = np.sqrt(mean_squared_error(y_test.iloc[:, i], preds[:, i]))
        mae = mean_absolute_error(y_test.iloc[:, i], preds[:, i])
        r2 = r2_score(y_test.iloc[:, i], preds[:, i])
        print(f"\nTarget: {target}")
        print(f"RMSE: {rmse:.2f}")
        print(f"MAE : {mae:.2f}")
        print(f"R²  : {r2:.3f}")

    return final_model, study

# ================================
# 6️⃣ Run training
# ================================
# df_final must already be loaded
# df_final = pd.read_csv("your_data.csv")
model, study = train_model(df_final, use_optuna=False)

print("\n✅ TRAINING FINISHED SUCCESSFULLY")
# 1. Re-create X and y for consistent splitting
X_all_features, y_all_targets = get_X_y(df_final)

# 2. Re-split the data to get X_test and y_test consistent with training
# We only need X_test and y_test for plotting, so we can discard X_trainval and y_trainval
_, X_test, _, y_test = train_test_split(
    X_all_features, y_all_targets, test_size=0.10, shuffle=False
)

# 3. Make predictions on the X_test using the trained 'model'
test_preds = model.predict(X_test)

# 4. Plotting
import matplotlib.pyplot as plt
import seaborn as sns

sns.set_style("whitegrid")

plt.figure(figsize=(15, 10))

for i, target in enumerate(TARGET_COLUMNS):
    plt.subplot(len(TARGET_COLUMNS), 1, i + 1) # Create subplots for each target
    plt.plot(y_test.index, y_test.iloc[:, i], label=f'Actual {target}', alpha=0.7)
    plt.plot(y_test.index, test_preds[:, i], label=f'Predicted {target}', linestyle='--', alpha=0.7)
    plt.title(f'Actual vs Predicted {target} on Test Set')
    plt.xlabel('Sample Index') # Using index as x-axis for alignment
    plt.ylabel(target)
    plt.legend()
    plt.grid(True)

plt.tight_layout()
plt.show()
import matplotlib.pyplot as plt
import seaborn as sns

sns.set_style("whitegrid")
plt.figure(figsize=(16, 8))

# Get the 'time' values corresponding to the test set indices
time_test = df_final.loc[y_test.index, 'time']

# Define colors for each target variable
colors = ['#1f77b4', '#ff7f0e', '#2ca02c'] # Blue, Orange, Green
linestyles = ['-', '--'] # Solid for actual, dashed for predicted

for i, target in enumerate(TARGET_COLUMNS):
    # Plot Actual values
    plt.plot(time_test, y_test.iloc[:, i],
             label=f'Actual {target}',
             color=colors[i],
             linestyle=linestyles[0], alpha=0.7)

    # Plot Predicted values
    plt.plot(time_test, test_preds[:, i],
             label=f'Predicted {target}',
             color=colors[i],
             linestyle=linestyles[1], alpha=0.7)

plt.title('Combined Actual vs Predicted Values on Test Set', fontsize=16, fontweight='bold')
plt.xlabel('Time', fontsize=12)
plt.ylabel('Value (Wh / SOC)', fontsize=12)
plt.legend(loc='upper left', bbox_to_anchor=(1, 1))
plt.grid(True, linestyle='--', alpha=0.6)
plt.xticks(rotation=45, ha='right') # Rotate x-axis labels for better readability
plt.tight_layout()
plt.show()
import joblib
import os
import matplotlib.pyplot as plt
import seaborn as sns

# Define the save directory
save_dir = '/content/drive/MyDrive/model2/LGBM2'
os.makedirs(save_dir, exist_ok=True)
print(f"Save directory created at: {save_dir}")

# 1. Save the trained model
model_path = os.path.join(save_dir, 'lgbm_multioutput_model.joblib')
joblib.dump(model, model_path)
print(f"Model saved to: {model_path}")

# --- Save Plots ---

# Ensure time_test, y_test, test_preds, and TARGET_COLUMNS are available from previous execution
# If this cell is run independently, these might need to be re-initialized.
# For continuity, assuming they are in the global scope.

# 2. Save the individual Actual vs Predicted plots
sns.set_style("whitegrid")
plt.figure(figsize=(15, 10))

for i, target in enumerate(TARGET_COLUMNS):
    plt.subplot(len(TARGET_COLUMNS), 1, i + 1) # Create subplots for each target
    plt.plot(y_test.index, y_test.iloc[:, i], label=f'Actual {target}', alpha=0.7)
    plt.plot(y_test.index, test_preds[:, i], label=f'Predicted {target}', linestyle='--', alpha=0.7)
    plt.title(f'Actual vs Predicted {target} on Test Set')
    plt.xlabel('Sample Index')
    plt.ylabel(target)
    plt.legend()
    plt.grid(True)

plt.tight_layout()
individual_plots_path = os.path.join(save_dir, 'individual_actual_vs_predicted_plots.png')
plt.savefig(individual_plots_path)
plt.close() # Close the figure to free up memory
print(f"Individual plots saved to: {individual_plots_path}")

# 3. Save the combined Actual vs Predicted plot with time on x-axis
sns.set_style("whitegrid")
plt.figure(figsize=(16, 8))

# Get the 'time' values corresponding to the test set indices (assuming df_final is available)
if 'df_final' in locals() or 'df_final' in globals():
    time_test = df_final.loc[y_test.index, 'time']
else:
    print("Warning: df_final not found, using y_test.index for combined plot.")
    time_test = y_test.index # Fallback if df_final isn't available

colors = ['#1f77b4', '#ff7f0e', '#2ca02c'] # Blue, Orange, Green
linestyles = ['-', '--'] # Solid for actual, dashed for predicted

for i, target in enumerate(TARGET_COLUMNS):
    plt.plot(time_test, y_test.iloc[:, i],
             label=f'Actual {target}',
             color=colors[i],
             linestyle=linestyles[0], alpha=0.7)
    plt.plot(time_test, test_preds[:, i],
             label=f'Predicted {target}',
             color=colors[i],
             linestyle=linestyles[1], alpha=0.7)

plt.title('Combined Actual vs Predicted Values on Test Set (Time Series)', fontsize=16, fontweight='bold')
plt.xlabel('Time', fontsize=12)
plt.ylabel('Value (Wh / SOC)', fontsize=12)
plt.legend(loc='upper left', bbox_to_anchor=(1, 1))
plt.grid(True, linestyle='--', alpha=0.6)
plt.xticks(rotation=45, ha='right')
plt.tight_layout()
combined_plot_path = os.path.join(save_dir, 'combined_actual_vs_predicted_time_series_plot.png')
plt.savefig(combined_plot_path)
plt.close() # Close the figure
print(f"Combined time-series plot saved to: {combined_plot_path}")

print("All requested items saved successfully!")






