"""Règles SEC1-6 locales (§4.6.6, CA-18.11, CA-18.34), copie fidèle de ``com.h2s.battery.rules.RuleEngine`` (brique 6).

Ordre de contrôle : domaine (action, puissance, SoC connu) → SEC1 conflit → SEC2/SEC3 SoC → SEC6 délai entre transitions
(seulement si l'action change l'état) → SEC4/SEC5 puissance ramenée au maximum, sans rejet. STANDBY est toujours admis :
c'est l'état sûr (D247).

Cahier des charges §5.2 (D310) : refus à la borne exacte — pas de décharge à SoC ≤ ``soc_min``, pas de charge à SoC ≥
``soc_max`` (R5-07) ; seuils jamais hors SoC 15–95 % ni sous 30 s entre transitions (R5-06, R5-08). Verdicts communs avec le
cloud : ``tests/vectors.json`` (``sec_rules``, ``sec_bounds``).
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

CHARGE, DISCHARGE, STANDBY = "CHARGE", "DISCHARGE", "STANDBY"

#: Bornes absolues du cahier des charges §5.2, comme ``AutoPolicy.SEC_SOC_MIN``, ``SEC_SOC_MAX``, ``SEC_MIN_INTERVAL_S``.
SEC_SOC_MIN = 15.0
SEC_SOC_MAX = 95.0
SEC_MIN_INTERVAL_S = 30


@dataclass(frozen=True)
class Thresholds:
    soc_min: float = 15.0
    soc_max: float = 95.0
    p_charge_max_w: float = 500.0
    p_discharge_max_w: float = 1000.0
    min_transition_interval_s: int = 30

    def __post_init__(self) -> None:
        if not (0 <= self.soc_min < self.soc_max <= 100):
            raise ValueError("seuils incohérents : soc_min / soc_max")
        if not (self.p_charge_max_w > 0 and self.p_discharge_max_w > 0):
            raise ValueError("seuils incohérents : puissances")
        if self.min_transition_interval_s < 0:
            raise ValueError("seuils incohérents : délai de transition")

    @classmethod
    def bounded(
        cls,
        soc_min: float,
        soc_max: float,
        p_charge_max_w: float,
        p_discharge_max_w: float,
        min_transition_interval_s: int,
    ) -> Thresholds:
        """Seuils reçus (cloud, base locale) ramenés dans les bornes du §5.2 : un réglage peut être plus strict, jamais plus large."""
        return cls(
            max(SEC_SOC_MIN, float(soc_min)),
            min(SEC_SOC_MAX, float(soc_max)),
            float(p_charge_max_w),
            float(p_discharge_max_w),
            max(SEC_MIN_INTERVAL_S, int(min_transition_interval_s)),
        )


@dataclass
class Result:
    valid: bool
    power_w: float
    rule: str | None = None
    message: str | None = None
    warnings: list[str] = field(default_factory=list)


def _fmt(v: float) -> str:
    r = round(v * 10) / 10
    return str(int(r)) if r == int(r) else str(r)


def validate(
    t: Thresholds,
    action: str,
    power_w: float,
    soc: float | None,
    now: dt.datetime,
    current_state: str | None,
    last_transition_at: dt.datetime | None,
) -> Result:
    state = current_state or STANDBY
    if action == STANDBY:
        return Result(True, 0.0)
    if action not in (CHARGE, DISCHARGE):
        return Result(False, 0.0, "REQUEST", f"Action inconnue : {action}")
    if power_w is None or not math.isfinite(power_w) or power_w <= 0:
        return Result(
            False,
            0.0,
            "REQUEST",
            "Puissance invalide : une charge ou une décharge exige une puissance finie et strictement positive.",
        )
    if soc is None or not math.isfinite(soc):
        return Result(False, 0.0, "REQUEST", "SoC inconnu : aucune mesure récente de la batterie.")
    if not 0 <= soc <= 100:
        return Result(False, 0.0, "REQUEST", "SoC invalide : valeur attendue entre 0 et 100 %.")
    if action == CHARGE and state == DISCHARGE:
        return Result(False, 0.0, "SEC1", "Conflit : impossible de charger pendant une décharge.")
    if action == DISCHARGE and state == CHARGE:
        return Result(False, 0.0, "SEC1", "Conflit : impossible de décharger pendant une charge.")
    if action == DISCHARGE and soc <= t.soc_min:
        return Result(
            False,
            0.0,
            "SEC2",
            f"SEC2 violation : SoC ({_fmt(soc)} %) ≤ {_fmt(t.soc_min)} % – Décharge impossible.",
        )
    if action == CHARGE and soc >= t.soc_max:
        return Result(
            False,
            0.0,
            "SEC3",
            f"SEC3 violation : SoC ({_fmt(soc)} %) ≥ {_fmt(t.soc_max)} % – Charge impossible.",
        )
    if action != state and last_transition_at is not None:
        elapsed = int((now - last_transition_at).total_seconds())
        if elapsed < t.min_transition_interval_s:
            return Result(
                False,
                0.0,
                "SEC6",
                f"SEC6 violation : délai minimum de {t.min_transition_interval_s} s non respecté ({max(0, elapsed)} s écoulées).",
            )
    limit = t.p_charge_max_w if action == CHARGE else t.p_discharge_max_w
    if power_w > limit:
        rule = "SEC4" if action == CHARGE else "SEC5"
        kind = "charge" if action == CHARGE else "décharge"
        return Result(
            True,
            float(limit),
            warnings=[f"{rule} : puissance de {kind} ajustée de {_fmt(power_w)} à {_fmt(limit)} W."],
        )
    return Result(True, float(power_w))
