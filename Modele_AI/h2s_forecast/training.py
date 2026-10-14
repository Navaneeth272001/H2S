"""Generic LightGBM training utilities (report §4.2–4.3).

* Bayesian hyper-parameter optimisation (Optuna TPE) on a chronological validation block
* Early stopping + L1/L2 regularisation + feature/row sub-sampling
* Time-series cross-validation (expanding window, purged gap) to report stability
* Final model refit on train+validation with the early-stopped number of trees
"""
from __future__ import annotations

import time
import warnings

import lightgbm as lgb
import numpy as np
import optuna
import pandas as pd
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.compose import TransformedTargetRegressor
import xgboost as xgb

from . import config as C

warnings.filterwarnings("ignore")
optuna.logging.set_verbosity(optuna.logging.WARNING)

BASE_PARAMS = {
    "objective": "regression",
    "boosting_type": "gbdt",
    "data_sample_strategy": "goss",   # Gradient-based One-Side Sampling
    "enable_bundle": True,            # Exclusive Feature Bundling (EFB)
    "random_state": C.RANDOM_STATE,
    "n_jobs": -1,
    "verbosity": -1,
}

XGB_BASE_PARAMS = {
    "tree_method": "hist",
    "random_state": C.RANDOM_STATE,
    "n_jobs": -1,
}


def metrics(y_true, y_pred) -> dict:
    y_true = np.asarray(y_true, float)
    y_pred = np.asarray(y_pred, float)
    mae = mean_absolute_error(y_true, y_pred)
    scale = np.mean(np.abs(y_true)) or 1.0
    return {
        "r2": float(r2_score(y_true, y_pred)),
        "rmse": float(np.sqrt(mean_squared_error(y_true, y_pred))),
        "mae": float(mae),
        "wape_pct": float(100 * np.sum(np.abs(y_true - y_pred)) / max(np.sum(np.abs(y_true)), 1e-9)),
        "nmae_pct": float(100 * mae / scale),
        "n": int(len(y_true)),
    }


def _suggest(trial: optuna.Trial) -> dict:
    return {
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.15, log=True),
        "num_leaves": trial.suggest_int("num_leaves", 8, 128, log=True),
        "max_depth": trial.suggest_int("max_depth", 3, 12),
        "min_child_samples": trial.suggest_int("min_child_samples", 10, 300, log=True),
        "colsample_bytree": trial.suggest_float("colsample_bytree", 0.4, 1.0),
        "reg_alpha": trial.suggest_float("reg_alpha", 1e-4, 10.0, log=True),
        "reg_lambda": trial.suggest_float("reg_lambda", 1e-4, 10.0, log=True),
        "min_split_gain": trial.suggest_float("min_split_gain", 0.0, 0.5),
    }


def fit_target(X_tr, y_tr, X_va, y_va, n_trials: int = C.N_OPTUNA_TRIALS, sample_weight=None) -> dict:
    """Tune, early-stop and refit one LightGBM regressor. Returns model + diagnostics."""
    t0 = time.time()

    def run(params):
        m = lgb.LGBMRegressor(n_estimators=4000, **BASE_PARAMS, **params)
        m.fit(X_tr, y_tr, sample_weight=sample_weight, eval_set=[(X_va, y_va)], eval_metric="l2",
              callbacks=[lgb.early_stopping(C.EARLY_STOPPING_ROUNDS, verbose=False)])
        return m

    def objective(trial):
        m = run(_suggest(trial))
        trial.set_user_attr("best_iter", int(m.best_iteration_ or m.n_estimators))
        return float(np.sqrt(mean_squared_error(y_va, m.predict(X_va))))

    if n_trials > 0:
        study = optuna.create_study(direction="minimize", sampler=optuna.samplers.TPESampler(seed=C.RANDOM_STATE))
        study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
        best = study.best_params
        best_iter = study.best_trial.user_attrs["best_iter"]
    else:
        best = {"learning_rate": 0.05, "num_leaves": 31, "max_depth": 8, "min_child_samples": 50,
                "colsample_bytree": 0.8, "reg_alpha": 0.1, "reg_lambda": 1.0, "min_split_gain": 0.0}
        best_iter = int(run(best).best_iteration_ or 500)

    # Validation diagnostics with the early-stopped model
    m_va = lgb.LGBMRegressor(n_estimators=best_iter, **BASE_PARAMS, **best).fit(X_tr, y_tr, sample_weight=sample_weight)
    val = metrics(y_va, m_va.predict(X_va))
    resid_va = np.asarray(y_va) - m_va.predict(X_va)

    # Final refit on train + validation, scaling the tree count with the data size
    X_full = pd.concat([X_tr, X_va])
    y_full = pd.concat([pd.Series(np.asarray(y_tr)), pd.Series(np.asarray(y_va))]).values
    n_final = int(best_iter * len(X_full) / max(len(X_tr), 1))
    sw = None
    if sample_weight is not None:
        sw = np.concatenate([np.asarray(sample_weight), np.ones(len(X_va))])
    model = lgb.LGBMRegressor(n_estimators=max(n_final, 50), **BASE_PARAMS, **best).fit(X_full, y_full, sample_weight=sw)
    return {
        "model": model,
        "params": {**best, "n_estimators": int(model.n_estimators)},
        "best_iter_valid": best_iter,
        "valid_metrics": val,
        "valid_pred": m_va.predict(X_va),
        "valid_residuals": resid_va,
        "train_seconds": round(time.time() - t0, 1),
    }


def _suggest_xgb(trial: optuna.Trial) -> dict:
    return {
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.1, log=True),
        "max_depth": trial.suggest_int("max_depth", 3, 10),
        "min_child_weight": trial.suggest_int("min_child_weight", 1, 20),
        "subsample": trial.suggest_float("subsample", 0.5, 1.0),
        "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
        "reg_lambda": trial.suggest_float("reg_lambda", 1e-3, 10.0, log=True),
        "reg_alpha": trial.suggest_float("reg_alpha", 1e-3, 10.0, log=True),
    }

def fit_xgboost_target(X_tr, y_tr, X_va, y_va, n_trials: int = C.N_OPTUNA_TRIALS, sample_weight=None) -> dict:
    """Tune, early-stop and refit one XGBoost regressor (with log1p target transform). Returns model + diagnostics."""
    t0 = time.time()
    y_tr_log = np.log1p(np.maximum(y_tr, 0))
    y_va_log = np.log1p(np.maximum(y_va, 0))

    def run(params):
        m = xgb.XGBRegressor(n_estimators=3000, early_stopping_rounds=C.EARLY_STOPPING_ROUNDS, **XGB_BASE_PARAMS, **params)
        m.fit(X_tr, y_tr_log, sample_weight=sample_weight, eval_set=[(X_va, y_va_log)], verbose=False)
        return m

    def objective(trial):
        m = run(_suggest_xgb(trial))
        trial.set_user_attr("best_iter", int(m.best_iteration))
        p_log = m.predict(X_va)
        p = np.expm1(p_log)
        return float(np.sqrt(mean_squared_error(y_va, p)))

    if n_trials > 0:
        study = optuna.create_study(direction="minimize", sampler=optuna.samplers.TPESampler(seed=C.RANDOM_STATE))
        study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
        best = study.best_params
        best_iter = study.best_trial.user_attrs["best_iter"]
    else:
        best = {"learning_rate": 0.05, "max_depth": 6, "min_child_weight": 5,
                "subsample": 0.8, "colsample_bytree": 0.8, "reg_lambda": 1.0, "reg_alpha": 0.0}
        best_iter = int(run(best).best_iteration)

    # Final refit on train + validation with target transformation
    X_full = pd.concat([X_tr, X_va])
    y_full = pd.concat([pd.Series(np.asarray(y_tr)), pd.Series(np.asarray(y_va))]).values
    n_final = int(best_iter * len(X_full) / max(len(X_tr), 1))
    sw = None
    if sample_weight is not None:
        sw = np.concatenate([np.asarray(sample_weight), np.ones(len(X_va))])

    final_base = xgb.XGBRegressor(n_estimators=max(n_final, 50), **XGB_BASE_PARAMS, **best)
    wrapped_model = TransformedTargetRegressor(regressor=final_base, func=np.log1p, inverse_func=np.expm1)
    wrapped_model.fit(X_full, y_full, sample_weight=sw)

    # For validation diagnostics, refit just on train to get valid predictions
    va_base = xgb.XGBRegressor(n_estimators=best_iter, **XGB_BASE_PARAMS, **best)
    va_wrapped = TransformedTargetRegressor(regressor=va_base, func=np.log1p, inverse_func=np.expm1)
    va_wrapped.fit(X_tr, y_tr, sample_weight=sample_weight)
    
    val_pred = va_wrapped.predict(X_va)
    val = metrics(y_va, val_pred)
    resid_va = np.asarray(y_va) - val_pred

    return {
        "model": wrapped_model,
        "params": {**best, "n_estimators": max(n_final, 50)},
        "best_iter_valid": best_iter,
        "valid_metrics": val,
        "valid_pred": val_pred,
        "valid_residuals": resid_va,
        "train_seconds": round(time.time() - t0, 1),
    }


def time_series_cv(X: pd.DataFrame, y, params: dict, algo: str = "lgbm", n_splits: int = C.N_CV_SPLITS, gap: int = 24) -> dict:
    """Expanding-window CV with a purge gap; reports mean ± std of R² and MAE across folds."""
    y = np.asarray(y)
    r2s, maes = [], []
    p = {k: v for k, v in params.items() if k != "n_estimators"}
    n_est = params.get("n_estimators", 500)
    for tr, va in TimeSeriesSplit(n_splits=n_splits, gap=gap).split(X):
        if algo == "xgb":
            m_base = xgb.XGBRegressor(n_estimators=n_est, **XGB_BASE_PARAMS, **p)
            m = TransformedTargetRegressor(regressor=m_base, func=np.log1p, inverse_func=np.expm1)
            m.fit(X.iloc[tr], y[tr])
        else:
            m = lgb.LGBMRegressor(n_estimators=n_est, **BASE_PARAMS, **p).fit(X.iloc[tr], y[tr])
        pr = m.predict(X.iloc[va])
        r2s.append(r2_score(y[va], pr))
        maes.append(mean_absolute_error(y[va], pr))
    return {"r2_mean": float(np.mean(r2s)), "r2_std": float(np.std(r2s)),
            "mae_mean": float(np.mean(maes)), "mae_std": float(np.std(maes)), "folds": [float(r) for r in r2s]}


def inference_latency(model, X: pd.DataFrame, repeats: int = 200) -> dict:
    """Median latency (ms) of a single-row prediction and of a 24-row batch."""
    row = X.iloc[[0]]
    batch = X.iloc[:24]
    model.predict(row)
    t = []
    for _ in range(repeats):
        s = time.perf_counter()
        model.predict(row)
        t.append(time.perf_counter() - s)
    tb = []
    for _ in range(max(repeats // 4, 10)):
        s = time.perf_counter()
        model.predict(batch)
        tb.append(time.perf_counter() - s)
    return {"single_ms": float(np.median(t) * 1000), "batch24_ms": float(np.median(tb) * 1000)}
