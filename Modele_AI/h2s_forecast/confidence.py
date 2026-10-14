"""Peak detection and confidence scoring (report §6).

Confidence score (MAE -> %)
---------------------------
For every prediction ``ŷ`` we look up the *expected absolute error* of the model in the
same operating regime, estimated on the chronological validation block (never on test):

    regime  = decile bin of ŷ (validation quantiles)
    MAE_bin = mean |y - ŷ| of validation samples in that bin
    score   = 100 * (1 - MAE_bin / max(|ŷ|, floor))   clipped to [0, 100]

``floor`` (= 10 % of the mean absolute target) prevents near-zero predictions (e.g. PV at
night, where the absolute error is also ~0) from producing meaningless scores.

Levels: High >= 85 %, Medium >= 70 %, Low < 70 %.
"""
from __future__ import annotations

import numpy as np

from . import config as C


def calibrate(y_valid_pred, residuals, n_bins: int = 10) -> dict:
    y_valid_pred = np.asarray(y_valid_pred, float)
    abs_res = np.abs(np.asarray(residuals, float))
    edges = np.unique(np.quantile(y_valid_pred, np.linspace(0, 1, n_bins + 1)))
    if len(edges) < 2:
        edges = np.array([y_valid_pred.min(), y_valid_pred.max() + 1e-9])
    bins = np.clip(np.searchsorted(edges, y_valid_pred, side="right") - 1, 0, len(edges) - 2)
    mae_bin = np.array([abs_res[bins == b].mean() if np.any(bins == b) else abs_res.mean()
                        for b in range(len(edges) - 1)])
    scale = float(np.mean(np.abs(y_valid_pred + np.asarray(residuals, float)))) or 1.0
    return {"edges": edges.tolist(), "mae_bin": mae_bin.tolist(), "mae_global": float(abs_res.mean()),
            "floor": 0.10 * scale, "scale": scale}


def score(y_pred, calib: dict) -> np.ndarray:
    y_pred = np.atleast_1d(np.asarray(y_pred, float))
    edges = np.asarray(calib["edges"])
    mae_bin = np.asarray(calib["mae_bin"])
    b = np.clip(np.searchsorted(edges, y_pred, side="right") - 1, 0, len(mae_bin) - 1)
    denom = np.maximum(np.abs(y_pred), calib["floor"])
    return np.clip(100.0 * (1.0 - mae_bin[b] / denom), 0.0, 100.0)


def level(conf) -> np.ndarray:
    conf = np.atleast_1d(np.asarray(conf, float))
    return np.where(conf >= C.CONFIDENCE_HIGH, "High", np.where(conf >= C.CONFIDENCE_MEDIUM, "Medium", "Low"))


def model_confidence(mae: float, scale: float) -> float:
    """Global model confidence = 100 - normalised MAE (%)."""
    return float(np.clip(100.0 * (1.0 - mae / max(scale, 1e-9)), 0.0, 100.0))


# ---------------------------------------------------------------------------
# Peaks
# ---------------------------------------------------------------------------
def peak_threshold(y_train, quantile: float = C.PEAK_QUANTILE) -> float:
    return float(np.quantile(np.asarray(y_train, float), quantile))


def detect_peaks(y_pred, threshold: float) -> np.ndarray:
    return np.asarray(y_pred, float) >= threshold


def peak_scores(y_true, y_pred, threshold: float) -> dict:
    t = np.asarray(y_true, float) >= threshold
    p = detect_peaks(y_pred, threshold)
    tp, fp, fn = int(np.sum(t & p)), int(np.sum(~t & p)), int(np.sum(t & ~p))
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"threshold": threshold, "n_true_peaks": int(t.sum()), "n_pred_peaks": int(p.sum()),
            "precision": prec, "recall": rec, "f1": f1}
