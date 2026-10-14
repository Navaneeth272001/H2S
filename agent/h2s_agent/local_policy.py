"""Règle de décision batterie du cahier des charges (§5.1, D310), embarquée pour le mode dégradé (§2.18, R5-20, C-9).

Copie de ``com.h2s.battery.service.AutoPolicy.decideCdc`` et de ``Thresholds.of`` (comme ``data/demo/regle_5_1.py``), vérifiée
par les vecteurs communs ``data/demo/vecteurs_regle_5_1.json`` (``tests/test_decisions_locales.py``) :

- CHARGE : surplus > surplus_min (50 W) ET SoC < soc_max (95 %) ET prix < price_low (0,12 €/kWh) ;
- DISCHARGE : (consommation > peak_threshold (300 W) OU prix > price_high (0,18 €/kWh)) ET SoC > soc_reserve (30 %) ;
- STANDBY sinon. Prix inconnu : aucune condition de prix remplie. La charge prime si les deux tiennent. La décharge couvre le
  déficit mesuré (jamais d'export), bornée par la consommation et la puissance de décharge. Capacité réservée : pas de décharge.

Le cloud reste le décideur en ligne ; l'agent n'applique cette règle que hors connexion, sans ordre du cloud en vigueur, puis
SEC1-6 locales tranchent (``rules.py``).

**Inférence LightGBM embarquée** (§2.18 « prédiction production 1h/24h, consommation 1h ») : point d'accroche ``Forecaster``.
La règle §5.1 ne lit que les grandeurs de l'instant ; aucun modèle LightGBM n'est livré à l'agent (modèle du client, Q-05) :
``NO_FORECAST`` dit pourquoi, sans prévision inventée.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Protocol

SEC_SOC_MIN = 15.0
SEC_SOC_MAX = 95.0
DEFAULTS = {
    "surplus_min": 50.0,
    "peak_threshold": 300.0,
    "soc_reserve": 30.0,
    "price_low": 0.12,
    "price_high": 0.18,
}


def _value(params: dict, key: str, fallback: float) -> float:
    v = params.get(key) if params else None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return fallback
    return v if math.isfinite(v) else fallback


def thresholds(
    site: dict,
    battery_soc_min: float = SEC_SOC_MIN,
    battery_soc_max: float = SEC_SOC_MAX,
) -> dict:
    """Seuils effectifs, comme AutoPolicy.Thresholds.of : le plus strict du profil et du site, jamais hors 15–95 %."""
    soc_min = max(SEC_SOC_MIN, max(battery_soc_min, _value(site, "soc_min", battery_soc_min)))
    soc_max = min(SEC_SOC_MAX, min(battery_soc_max, _value(site, "soc_max", battery_soc_max)))
    return {
        "surplus_min": _value(site, "surplus_min", DEFAULTS["surplus_min"]),
        "peak_threshold": _value(site, "peak_threshold", DEFAULTS["peak_threshold"]),
        "soc_reserve": max(soc_min, _value(site, "soc_reserve", DEFAULTS["soc_reserve"])),
        "soc_min": soc_min,
        "soc_max": soc_max,
        "price_low": _value(site, "price_low", DEFAULTS["price_low"]),
        "price_high": _value(site, "price_high", DEFAULTS["price_high"]),
    }


def decide(
    soc: float,
    surplus_w: float,
    consumption_w: float,
    t: dict,
    p_charge_max_w: float,
    p_discharge_max_w: float,
    price_eur_kwh: float | None,
    flexibility_reserved: bool = False,
) -> tuple[str, float]:
    """(action, puissance W) selon §5.1."""
    low = price_eur_kwh is not None and price_eur_kwh < t["price_low"]
    high = price_eur_kwh is not None and price_eur_kwh > t["price_high"]
    if surplus_w > t["surplus_min"] and soc < t["soc_max"] and low:
        return "CHARGE", min(surplus_w, p_charge_max_w)
    if (consumption_w > t["peak_threshold"] or high) and soc > t["soc_reserve"] and not flexibility_reserved:
        power = max(0.0, min(min(-surplus_w, max(0.0, consumption_w)), p_discharge_max_w))
        return ("DISCHARGE", power) if power > 0 else ("STANDBY", 0.0)
    return "STANDBY", 0.0


def manual_override(policy: dict | None) -> set[int]:
    """Batteries du site sous mode manuel (§2.5), telles que le cloud les transmet dans la politique locale (V2).

    Entrées illisibles ignorées ; un cloud antérieur au lot 3 n'envoie rien, la liste est alors vide (compatibilité
    ascendante). Le mode manuel du technicien posé sur le boîtier lui-même reste distinct (``DecisionManager.manual_mode``).
    """
    out: set[int] = set()
    if not isinstance(policy, dict):
        return out
    for s in policy.get("manual_override") or []:
        try:
            out.add(int(s))
        except (TypeError, ValueError):
            continue
    return out


def local_decision_allowed(policy: dict | None, stock: int) -> bool:
    """L'agent peut-il décider seul de cette batterie hors réseau (V2) ?

    Même prédicat que ``EdgeDtos.LocalPolicy#localDecisionAllowed`` côté cloud, vérifié sur les vecteurs communs
    ``data/demo/vecteurs_mode_manuel_local.json`` : pilotage automatique du site autorisé **et** aucun mode manuel posé
    depuis la plateforme sur cette batterie. Un « FORCER STOP » posé avant une coupure du lien ne doit pas être levé par
    la coupure elle-même.
    """
    if not isinstance(policy, dict) or not policy.get("auto_pilot"):
        return False
    return stock not in manual_override(policy)


class Forecaster(Protocol):
    """Prévisions locales (production 1 h / 24 h, consommation 1 h) d'un modèle embarqué ; ``None`` sans modèle."""

    def forecast(self, now: dt.datetime) -> dict | None: ...

    def status(self) -> dict: ...


class NoForecaster:
    """Aucun modèle LightGBM embarqué : aucune prévision, et la raison est exposée (statut de l'agent)."""

    REASON = (
        "modèle LightGBM embarqué non livré (modèle du client, Q-05) : décisions locales sur les mesures de l'instant"
    )

    def forecast(self, now: dt.datetime) -> dict | None:
        return None

    def status(self) -> dict:
        return {"available": False, "reason": self.REASON}


NO_FORECAST = NoForecaster()
