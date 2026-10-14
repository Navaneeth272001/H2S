"""End-to-end training of the H2S prediction models (report §4–§6).

Usage (from ``Modele_AI/``)::

    python -m h2s_forecast.pipeline               # full run (Optuna tuning + baselines)
    python -m h2s_forecast.pipeline --fast        # no tuning, no RNN baselines (smoke test)

Outputs: ``trained_models/<name>.pkl`` bundles, ``trained_models/predictions/<name>.csv`` test
predictions, and ``trained_models/metrics.json`` consumed by the report generator.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time

import joblib
import lightgbm as lgb
import xgboost as xgb
import numpy as np
import pandas as pd
from sklearn.compose import TransformedTargetRegressor

from . import baselines as B
from . import confidence as CF
from . import config as C
from . import data as D
from . import external as E
from . import features as F
from . import training as T

VERSION = "2.0.0"
UNITS = {"production_wh": "Wh", "energy_total_wh": "Wh", "price_eur_mwh": "EUR/MWh",
         "temperature": "°C", "humidity": "%", "wind": "m/s", "radiation": "W/m²"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def exo_masks(index: pd.DatetimeIndex, horizon_h: int):
    """Chronological split for exogenous models with a purge of `horizon_h` hours between blocks."""
    purge = pd.Timedelta(hours=horizon_h)
    tr = index < pd.Timestamp(C.EXO_VALID_START) - purge
    va = (index >= pd.Timestamp(C.EXO_VALID_START)) & (index < pd.Timestamp(C.EXO_TEST_START) - purge)
    te = index >= pd.Timestamp(C.EXO_TEST_START)
    return tr, va, te


def ratio_masks(n: int, purge: int):
    i1, i2 = int(n * C.SPLIT_TRAIN), int(n * (C.SPLIT_TRAIN + C.SPLIT_VALID))
    tr = np.zeros(n, bool)
    va = np.zeros(n, bool)
    te = np.zeros(n, bool)
    tr[: max(i1 - purge, 0)] = True
    va[i1: max(i2 - purge, i1)] = True
    te[i2:] = True
    return tr, va, te


def augment(X: pd.DataFrame, y: pd.Series, cols: list[str], frac: float = 0.2, seed: int = C.RANDOM_STATE):
    """Append a masked copy of `frac` of the rows (cols -> NaN) so the trees learn a missing-input branch."""
    if not cols or frac <= 0:
        return X, y
    rng = np.random.default_rng(seed)
    pick = rng.choice(len(X), int(len(X) * frac), replace=False)
    Xm = X.iloc[pick].copy()
    Xm[cols] = np.nan
    return pd.concat([X, Xm]), pd.concat([y, y.iloc[pick]])


def importance(model, features: list[str], top: int = 15) -> list[dict]:
    if hasattr(model, "regressor_"):
        m = model.regressor_
    else:
        m = model
        
    if hasattr(m, "booster_"):
        gain = m.booster_.feature_importance(importance_type="gain")
    elif hasattr(m, "feature_importances_"):
        gain = m.feature_importances_
    else:
        return []
    s = pd.Series(gain / max(gain.sum(), 1e-9) * 100, index=features).sort_values(ascending=False).head(top)
    return [{"feature": k, "gain_pct": round(float(v), 2)} for k, v in s.items()]


def confidence_report(y_true, y_pred, calib) -> dict:
    conf = CF.score(y_pred, calib)
    lv = CF.level(conf)
    out = {"mean_confidence": float(conf.mean())}
    for name in ("High", "Medium", "Low"):
        m = lv == name
        if m.any():
            err = np.abs(np.asarray(y_true)[m] - np.asarray(y_pred)[m])
            out[name] = {"share_pct": float(100 * m.mean()), "mae": float(err.mean()),
                         "nmae_pct": float(100 * err.sum() / max(np.abs(np.asarray(y_true)[m]).sum(), 1e-9))}
        else:
            out[name] = {"share_pct": 0.0, "mae": None, "nmae_pct": None}
    return out


def train_bundle(name: str, family: str, horizon_h: int, X: pd.DataFrame, Y: pd.DataFrame, masks, n_trials: int,
                 degrade_cols: list[str], eval_mask=None, extra: dict | None = None, algo: str = "lgbm") -> tuple[dict, pd.DataFrame]:
    """Train one model per target column, evaluate, calibrate confidence and save the bundle."""
    tr, va, te = masks
    ev = te if eval_mask is None else eval_mask
    print(f"\n=== {name}  (train={tr.sum()}, valid={va.sum()}, test={ev.sum()}, features={X.shape[1]}, algo={algo})")
    bundle = {"name": name, "version": VERSION, "family": family, "horizon_h": horizon_h,
              "targets": list(Y.columns), "units": {t: UNITS.get(t, "") for t in Y.columns},
              "features": list(X.columns), "degraded_columns": degrade_cols, "models": {}, "params": {},
              "calibration": {}, "peak_threshold": {}, "trained_at": dt.datetime.utcnow().isoformat() + "Z",
              "train_period": [str(X.index[tr].min()), str(X.index[va].max())],
              "lightgbm_version": lgb.__version__, "algo": algo}
    report = {"targets": {}, "horizon_h": horizon_h, "family": family, "n_features": X.shape[1],
              "n_train": int(tr.sum()), "n_valid": int(va.sum()), "n_test": int(ev.sum()),
              "test_period": [str(X.index[ev].min()), str(X.index[ev].max())]}
    preds = pd.DataFrame(index=X.index[ev])
    for t in Y.columns:
        y = Y[t]
        Xtr, ytr = augment(X[tr], y[tr], degrade_cols)
        if algo == "xgb":
            res = T.fit_xgboost_target(Xtr, ytr, X[va], y[va], n_trials=n_trials)
        else:
            res = T.fit_target(Xtr, ytr, X[va], y[va], n_trials=n_trials)
        model = res["model"]
        p = model.predict(X[ev])
        Xd = X[ev].copy()
        if degrade_cols:
            Xd[degrade_cols] = np.nan
        pd_ = model.predict(Xd)
        calib = CF.calibrate(res["valid_pred"], res["valid_residuals"])
        thr = CF.peak_threshold(y[tr])
        r = {
            "test": T.metrics(y[ev], p),
            "valid": res["valid_metrics"],
            "degraded": T.metrics(y[ev], pd_) if degrade_cols else None,
            "params": res["params"],
            "best_iter_valid": res["best_iter_valid"],
            "train_seconds": res["train_seconds"],
            "importance": importance(model, list(X.columns)),
            "confidence": confidence_report(y[ev], p, calib),
            "model_confidence_pct": CF.model_confidence(T.metrics(y[ev], p)["mae"], float(np.mean(np.abs(y[ev])))),
            "peaks": CF.peak_scores(y[ev], p, thr),
            "latency": T.inference_latency(model, X[ev]),
            "target_stats": {"mean": float(y.mean()), "std": float(y.std()), "min": float(y.min()), "max": float(y.max())},
        }
        report["targets"][t] = r
        bundle["models"][t] = model
        bundle["params"][t] = res["params"]
        bundle["calibration"][t] = calib
        bundle["peak_threshold"][t] = thr
        preds[f"{t}_true"] = y[ev].values
        preds[f"{t}_pred"] = p
        preds[f"{t}_pred_degraded"] = pd_
        preds[f"{t}_conf"] = CF.score(p, calib)
        m = r["test"]
        print(f"  {t:18s} R2={m['r2']:.3f} RMSE={m['rmse']:.2f} MAE={m['mae']:.2f} | valid R2={r['valid']['r2']:.3f}"
              + (f" | degraded R2={r['degraded']['r2']:.3f}" if r["degraded"] else "")
              + f" | {res['train_seconds']}s")
    if extra:
        report.update(extra)
    bundle["metrics"] = report
    C.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, C.MODELS_DIR / f"{name}.pkl", compress=3)
    (C.MODELS_DIR / "predictions").mkdir(exist_ok=True)
    preds.to_csv(C.MODELS_DIR / "predictions" / f"{name}.csv", index_label="time")
    return report, preds


# ---------------------------------------------------------------------------
# Families
# ---------------------------------------------------------------------------
def perturb_weather(X: pd.DataFrame, sigma: float, rng) -> pd.DataFrame:
    """Multiplicative Gaussian noise on NWP radiation / wind at target time (derived columns recomputed)."""
    Xp = X.copy()
    rad = (Xp["tgt_radiation_fc"] * (1 + rng.normal(0, sigma, len(Xp)))).clip(lower=0)
    wind = (Xp["tgt_wind_fc"] * (1 + rng.normal(0, sigma, len(Xp)))).clip(lower=0)
    Xp["tgt_radiation_fc"], Xp["tgt_wind_fc"] = rad, wind
    Xp["tgt_prod_physics_fc"] = C.P_PV_W * rad / 1000 + D.wind_power(wind.values)
    Xp["tgt_wind_cubed_fc"] = np.clip(wind, 0, C.V_NOM) ** 3
    Xp["tgt_clearness_fc"] = rad / np.maximum(Xp["tgt_clear_sky"], 20)
    return Xp


def run_production(obs, fc, n_trials, results, frames):
    for h in (1, 24):
        X, y, _ = F.production_dataset(obs, fc, h)
        masks = exo_masks(X.index, h)
        # Oracle (perfect weather) upper bound
        Xo, yo, _ = F.production_dataset(obs, fc, h, oracle=True)
        mo = exo_masks(Xo.index, h)
        ro = T.fit_target(Xo[mo[0]], yo[mo[0]], Xo[mo[1]], yo[mo[1]], n_trials=0)
        oracle = T.metrics(yo[mo[2]], ro["model"].predict(Xo[mo[2]]))
        rep, pr = train_bundle(f"production_{h}h", "production", h, X, y.to_frame(), masks, n_trials,
                               F.nwp_columns(X), extra={"oracle_test": oracle})
        # Sensitivity to weather-forecast noise
        bundle = joblib.load(C.MODELS_DIR / f"production_{h}h.pkl")
        model = bundle["models"]["production_wh"]
        rng = np.random.default_rng(C.RANDOM_STATE)
        sens = []
        for s in (0.0, 0.1, 0.2, 0.3, 0.5):
            r2 = np.mean([T.metrics(y[masks[2]], model.predict(perturb_weather(X[masks[2]], s, rng)))["r2"]
                          for _ in range(5)])
            sens.append({"sigma_pct": int(s * 100), "r2": float(r2)})
        rep["weather_noise_sensitivity"] = sens
        results[f"production_{h}h"] = rep
        frames[f"production_{h}h"] = (X, y, masks)
        print(f"  oracle R2={oracle['r2']:.4f} | noise sensitivity {[(d['sigma_pct'], round(d['r2'], 3)) for d in sens]}")


def run_weather(obs, fc, n_trials, results):
    for h in (1, 24):
        X, Y, _ = F.weather_dataset(obs, fc, h)
        masks = exo_masks(X.index, h)
        lead = "fc0" if h == 1 else "fc1"
        nwp = {}
        for t in Y.columns:
            raw = fc[f"{t}_{lead}"].reindex(X.index[masks[2]] + pd.Timedelta(hours=h)).values
            ok = ~np.isnan(raw)
            nwp[t] = T.metrics(Y[t][masks[2]].values[ok], raw[ok])
        rep, _ = train_bundle(f"weather_{h}h", "weather", h, X, Y, masks, n_trials, F.nwp_columns(X),
                              extra={"raw_nwp_test": nwp})
        results[f"weather_{h}h"] = rep


def run_price(prices, obs, fc, n_trials, results):
    for h in (1, 24):
        X, y, _ = F.price_dataset(prices, obs, fc, h)
        masks = exo_masks(X.index, h)
        te = masks[2]
        naive = {"persistence": T.metrics(y[te], X["price_lag0"][te]),
                 "same_hour_yesterday": T.metrics(y[te], X["price_tgt_minus24h"][te])}
        rep, _ = train_bundle(f"price_{h}h", "price", h, X, y.to_frame(), masks, n_trials, F.nwp_columns(X),
                              extra={"naive_test": naive})
        results[f"price_{h}h"] = rep


def walk_forward_consumption(X, y, meta, params, start_frac: float = 0.7, h: int = 60, algo: str = "lgbm") -> dict:
    """Daily expanding-window refit over the last 30 % (mirrors a daily retraining schedule)."""
    days = pd.Series(X.index.normalize().unique())
    first = X.index[int(len(X) * start_frac)].normalize()
    p = {k: v for k, v in params.items() if k != "n_estimators"}
    yt, yp = [], []
    for d in days[days >= first]:
        tr = X.index < d - pd.Timedelta(minutes=h)
        te = (X.index >= d) & (X.index < d + pd.Timedelta(days=1)) & meta["hourly_origin"].values
        if te.sum() == 0 or tr.sum() < 1000:
            continue
        if algo == "xgb":
            m_base = xgb.XGBRegressor(n_estimators=params["n_estimators"], **T.XGB_BASE_PARAMS, **p)
            m = TransformedTargetRegressor(regressor=m_base, func=np.log1p, inverse_func=np.expm1).fit(X[tr], y[tr])
        else:
            m = lgb.LGBMRegressor(n_estimators=params["n_estimators"], **T.BASE_PARAMS, **p).fit(X[tr], y[tr])
        yt.append(y[te].values)
        yp.append(m.predict(X[te]))
    yt, yp = np.concatenate(yt), np.concatenate(yp)
    return {**T.metrics(yt, yp), "n_days": int(len(days[days >= first]))}


def run_consumption(obs, n_trials, results, frames):
    wide, valid = D.minute_power()
    hourly = D.hourly_consumption(wide, valid)
    results["_data"] = {
        "n_raw_rows": int(len(D.load_raw_energy())), "n_devices": int(wide.shape[1]), "devices": list(wide.columns),
        "minute_grid": int(len(wide)), "minute_valid_pct": float(100 * valid.mean()),
        "hours": int(len(hourly)), "hours_valid_pct": float(100 * hourly["valid"].mean()),
        "period": [str(wide.index.min()), str(wide.index.max())],
    }
    for h, name in ((60, "consumption_1h"), (1440, "consumption_24h_fallback")):
        X, y, meta = F.consumption_dataset(wide, valid, obs, h)
        ok = meta["y_valid"].values
        X, y, meta = X[ok], y[ok], meta[ok]
        masks = ratio_masks(len(X), purge=h)
        hourly_test = masks[2] & meta["hourly_origin"].values
        te_all = masks[2]
        naive = {"persistence_last60": T.metrics(y[hourly_test], X["e_last_60m"][hourly_test]),
                 "persistence_power15": T.metrics(y[hourly_test], X["p_mean_15m"][hourly_test]),
                 "seasonal_naive_24h": T.metrics(y[hourly_test], X["e_target_lag24h"][hourly_test].fillna(X["e_last_60m"][hourly_test]))}
        rep, _ = train_bundle(name, "consumption", h // 60, X, y.to_frame(), masks, n_trials,
                              F.recent_sensor_columns(X), eval_mask=hourly_test, extra={"naive_test": naive}, algo="xgb")
        bundle = joblib.load(C.MODELS_DIR / f"{name}.pkl")
        model = bundle["models"]["energy_total_wh"]
        rep["test_all_minute_origins"] = T.metrics(y[te_all], model.predict(X[te_all]))
        if h == 60:
            rep["walk_forward"] = walk_forward_consumption(X, y, meta, bundle["params"]["energy_total_wh"], algo="xgb")
            print(f"  walk-forward (daily refit) R2={rep['walk_forward']['r2']:.3f} n={rep['walk_forward']['n']}")
            rep["cv"] = T.time_series_cv(X[masks[0] | masks[1]], y[masks[0] | masks[1]],
                                         bundle["params"]["energy_total_wh"], gap=h, algo="xgb")
        results[name] = rep
        frames[name] = (X, y, (masks[0], masks[1], hourly_test))


def run_baselines(frames, results, rnn: bool = True):
    """XGBoost / LSTM / GRU / naive on the same splits as LightGBM (§5.4)."""
    for name in ("production_1h", "production_24h", "consumption_1h"):
        X, y, (tr, va, te) = frames[name]
        y = pd.Series(np.asarray(y).ravel(), index=X.index)
        out = {}
        lgb_m = joblib.load(C.MODELS_DIR / f"{name}.pkl")
        tgt = lgb_m["targets"][0]
        t0 = time.time()
        p = lgb_m["models"][tgt].predict(X[te])
        out["LightGBM"] = {**T.metrics(y[te], p), "train_seconds": lgb_m["metrics"]["targets"][tgt]["train_seconds"],
                           "infer_ms_per_row": 1000 * (time.time() - t0) / te.sum()}
        px, sec = B.xgboost_baseline(X[tr], y[tr], X[va], y[va], X[te])
        out["XGBoost"] = {**T.metrics(y[te], px), "train_seconds": round(sec, 1)}
        if rnn:
            pos = np.arange(len(X))
            stride = 60 if name.startswith("consumption") else 1
            L = 12 if name.startswith("consumption") else 24
            ptr = pos[tr][::5] if name.startswith("consumption") else pos[tr]
            for cell in ("lstm", "gru"):
                pr, sec = B.rnn_baseline(X, y.values, ptr, pos[va], pos[te], cell, L=L, stride=stride)
                out[cell.upper()] = {**T.metrics(y[te], pr), "train_seconds": round(sec, 1)}
        results[name]["baselines"] = out
        print(f"  baselines {name}: " + ", ".join(f"{k}={v['r2']:.3f}" for k, v in out.items()))


# ---------------------------------------------------------------------------
def main(fast: bool = False):
    t0 = time.time()
    n_trials = 0 if fast else C.N_OPTUNA_TRIALS
    obs, fc, prices = E.weather_observed(), E.weather_forecast(), E.spot_prices()
    results: dict = {"_meta": {"version": VERSION, "generated_at": dt.datetime.utcnow().isoformat() + "Z",
                               "optuna_trials": n_trials, "lightgbm": lgb.__version__,
                               "history": [C.HISTORY_START, C.HISTORY_END],
                               "exo_split": {"train_end": C.EXO_VALID_START, "test_start": C.EXO_TEST_START}}}
    frames: dict = {}
    run_consumption(obs, n_trials, results, frames)
    run_production(obs, fc, n_trials, results, frames)
    run_weather(obs, fc, n_trials, results)
    run_price(prices, obs, fc, n_trials, results)
    run_baselines(frames, results, rnn=not fast)
    results["_meta"]["total_seconds"] = round(time.time() - t0, 1)
    C.METRICS_FILE.write_text(json.dumps(results, indent=2, default=float))
    print(f"\nDone in {results['_meta']['total_seconds']} s -> {C.METRICS_FILE}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true")
    main(ap.parse_args().fast)
