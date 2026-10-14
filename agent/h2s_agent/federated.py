"""Contribution à l'apprentissage fédéré (F18.4, CA-18.16 à CA-18.20, D253).

Modèle local : correction linéaire de la prévision de consommation horaire du site (kWh), sur 7 jours d'agrégats. Les
poids sont ajustés autour du modèle global par une régression ridge « rappelée » vers lui (w = argmin ‖Xw − y‖² +
λ‖w − w₀‖²) ; seule la mise à jour w − w₀ part, chiffrée pour le cloud. Aucune mesure brute ne quitte l'agent.
Calcul en Python pur : 8 variables, résolution d'un système 8×8.

Variables (ordre partagé avec ``com.h2s.edge.model.FederatedMath.FEATURES``) :
bias, hour_sin, hour_cos, weekday_sin, weekday_cos, is_weekend, lag_1h, lag_24h.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import uuid
from collections.abc import Callable

from .crypto import encrypt_for_cloud, verify_signed_payload
from .storage import Database

FEATURES = [
    "bias",
    "hour_sin",
    "hour_cos",
    "weekday_sin",
    "weekday_cos",
    "is_weekend",
    "lag_1h",
    "lag_24h",
]
MODEL_TYPE = "consumption_1h"
LAMBDA = 1.0
MIN_SAMPLES = 48


def features(at: dt.datetime, lag_1h: float, lag_24h: float) -> list[float]:
    h = at.hour + at.minute / 60
    wd = at.weekday()
    return [
        1.0,
        math.sin(2 * math.pi * h / 24),
        math.cos(2 * math.pi * h / 24),
        math.sin(2 * math.pi * wd / 7),
        math.cos(2 * math.pi * wd / 7),
        1.0 if wd >= 5 else 0.0,
        lag_1h,
        lag_24h,
    ]


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Élimination de Gauss avec pivot partiel ; lève ``ValueError`` si le système est singulier."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            raise ValueError("système singulier")
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x


def train(samples: list[tuple[list[float], float]], w0: list[float], lam: float = LAMBDA) -> tuple[list[float], float]:
    """Ridge rappelée vers ``w0`` : (XᵀX + λI) w = Xᵀy + λ w0. Renvoie les poids et l'erreur quadratique moyenne."""
    n = len(FEATURES)
    xtx = [[0.0] * n for _ in range(n)]
    xty = [0.0] * n
    for x, y in samples:
        for i in range(n):
            xty[i] += x[i] * y
            for j in range(n):
                xtx[i][j] += x[i] * x[j]
    for i in range(n):
        xtx[i][i] += lam
        xty[i] += lam * w0[i]
    w = solve(xtx, xty)
    mse = sum((sum(wi * xi for wi, xi in zip(w, x)) - y) ** 2 for x, y in samples) / max(1, len(samples))
    return w, mse


class Federated:
    def __init__(self, db: Database, now: Callable[[], dt.datetime], site: Callable[[], str]) -> None:
        self._db = db
        self._now = now
        self._site = site

    def dataset(self, end: dt.datetime, days: int = 7) -> list[tuple[list[float], float]]:
        """Consommation horaire du site (somme des puissances moyennes des équipements, kWh) sur ``days`` jours."""
        start = end - dt.timedelta(days=days + 1)
        with self._db.tx() as cur:
            cur.execute(
                "SELECT period_start, sum(avg_value) / 1000.0 AS kwh FROM local_aggregated WHERE aggregation_period = '1h'"
                " AND data_type = 'power_w' AND period_start >= %s AND period_start < %s GROUP BY period_start ORDER BY period_start",
                (start, end),
            )
            series = {r["period_start"]: float(r["kwh"]) for r in cur.fetchall()}
        samples = []
        for at, y in series.items():
            if at < end - dt.timedelta(days=days):
                continue
            lag1 = series.get(at - dt.timedelta(hours=1))
            lag24 = series.get(at - dt.timedelta(hours=24))
            if lag1 is None or lag24 is None:
                continue
            samples.append((features(at, lag1, lag24), y))
        return samples

    def active_model(self) -> tuple[str, list[float]]:
        with self._db.tx() as cur:
            cur.execute(
                "SELECT model_version, weights FROM local_models WHERE model_type = %s AND is_active ORDER BY downloaded_at DESC LIMIT 1",
                (MODEL_TYPE,),
            )
            r = cur.fetchone()
        if r is None or not r["weights"]:
            return "federated-0", [0.0] * len(FEATURES)
        return r["model_version"], [float(v) for v in r["weights"]]

    def contribution(self, gradient_public_key: str) -> dict | None:
        """Contribution prête à envoyer, conservée dans ``local_gradients`` ; ``None`` sans données suffisantes (CA-18.16, CA-18.17)."""
        now = self._now()
        samples = self.dataset(now)
        if len(samples) < MIN_SAMPLES:
            return None
        version, w0 = self.active_model()
        w, mse = train(samples, w0)
        delta = [round(a - b, 9) for a, b in zip(w, w0)]
        clear = json.dumps({"features": FEATURES, "delta": delta}, separators=(",", ":")).encode("utf-8")
        message_id = str(uuid.uuid4())
        first, last = now - dt.timedelta(days=7), now
        body = {
            "message_id": message_id,
            "model_type": MODEL_TYPE,
            "base_version": version,
            "sample_count": len(samples),
            "loss": round(mse, 6),
            "data_from": first.isoformat().replace("+00:00", "Z"),
            "data_to": last.isoformat().replace("+00:00", "Z"),
            "encrypted": encrypt_for_cloud(gradient_public_key, clear),
        }
        with self._db.tx() as cur:
            cur.execute(
                "INSERT INTO local_gradients (site_id, round_id, model_version, model_type, gradient_data, sample_count, data_range_start,"
                " data_range_end, loss, message_id, created_at) VALUES (%s, 0, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    self._site(),
                    version,
                    MODEL_TYPE,
                    json.dumps(body),
                    len(samples),
                    first,
                    last,
                    mse,
                    message_id,
                    now,
                ),
            )
        return body

    def install(self, signed: dict, decision_public_key: str) -> bool:
        """Modèle global vérifié (signature, type, forme) puis installé et activé (CA-18.20). Faux : écarté, ``ValueError``."""
        payload = signed.get("payload") if isinstance(signed, dict) else None
        if not isinstance(payload, str):
            raise ValueError("modèle mal formé")  # noqa: TRY004 - contenu mal formé, pas un type d'argument
        m = verify_signed_payload(decision_public_key, payload, str(signed.get("signature")))
        if m.get("type") != "model" or m.get("model_type") != MODEL_TYPE or m.get("features") != FEATURES:
            raise ValueError("modèle d'un autre type ou d'autres variables")
        weights = m.get("weights")
        if (
            not isinstance(weights, list)
            or len(weights) != len(FEATURES)
            or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in weights)
        ):
            raise ValueError("poids invalides")
        sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self._db.tx() as cur:
            cur.execute(
                "SELECT 1 FROM local_models WHERE model_type = %s AND model_version = %s AND model_hash = %s",
                (MODEL_TYPE, m["version"], sha),
            )
            if cur.fetchone():
                return False
            cur.execute(
                "UPDATE local_models SET is_active = FALSE WHERE model_type = %s",
                (MODEL_TYPE,),
            )
            cur.execute(
                "INSERT INTO local_models (site_id, model_version, model_type, model_path, model_hash, weights, payload, signature, is_active,"
                " downloaded_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s) ON CONFLICT ON CONSTRAINT unique_local_model DO UPDATE"
                " SET model_hash = EXCLUDED.model_hash, weights = EXCLUDED.weights, payload = EXCLUDED.payload, signature = EXCLUDED.signature,"
                " is_active = TRUE, downloaded_at = EXCLUDED.downloaded_at",
                (
                    self._site(),
                    m["version"],
                    MODEL_TYPE,
                    "db:local_models",
                    sha,
                    json.dumps(weights),
                    payload,
                    signed.get("signature"),
                    self._now(),
                ),
            )
        return True
