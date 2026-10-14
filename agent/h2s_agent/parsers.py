"""Lecture des messages MQTT des équipements (F18.1, CA-18.1, CA-18.5, D255, D256).

Chaque message devient des relevés typés, validés sur des plages physiques, horodatés en UTC. Un message illisible ou
hors plage est écarté et compté, jamais écrit. Date absente ou incohérente (±24 h de l'horloge de l'agent) : heure de
réception, qualité ``ESTIMATED`` ; horloge de l'agent non synchronisée : qualité ``ESTIMATED`` aussi (D256).
"""

from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass

RANGES = {
    "power_w": (-1_000_000, 1_000_000, "W"),
    "current_a": (-5_000, 5_000, "A"),
    "voltage_v": (0, 1_000, "V"),
    "energy_total_wh": (0, 1e12, "Wh"),
    "energy_wh": (0, 99_999_999, "Wh"),
    "power_factor": (-1, 1, ""),
    "soc_pct": (0, 100, "%"),
    "power_charge_w": (0, 1_000_000, "W"),
    "power_discharge_w": (0, 1_000_000, "W"),
    "temperature_c": (-40, 120, "°C"),
    "irradiance_w_m2": (0, 2_000, "W/m²"),
    "wind_speed_m_s": (0, 100, "m/s"),
    "humidity_pct": (0, 100, "%"),
    "rain_mm": (0, 500, "mm"),
    # Linky (TIC, E172) : puissance apparente et production, gardées en local (le cloud ne les reçoit pas agrégées).
    "apparent_power_va": (0, 1_000_000, "VA"),
    "apparent_power_injected_va": (0, 1_000_000, "VA"),
    "energy_injected_total_wh": (0, 1e12, "Wh"),
}


@dataclass(frozen=True)
class Reading:
    source: str  # device | battery | weather
    ref: int | None  # device_id, stock_id, ou None pour la météo
    data_type: str
    value: float
    at: dt.datetime
    quality: str = "OK"

    @property
    def device_key(self) -> str:
        return {
            "device": f"device:{self.ref}",
            "battery": f"stock:{self.ref}",
            "weather": "weather",
        }[self.source]

    @property
    def unit(self) -> str:
        return RANGES[self.data_type][2]


class ParseError(ValueError):
    pass


def _json(payload: bytes) -> dict:
    if len(payload) > 16_384:
        raise ParseError("message trop volumineux")
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParseError("JSON illisible") from exc
    if not isinstance(data, dict):
        raise ParseError("objet JSON attendu")
    return data


def _when(value: object, received: dt.datetime, clock_synced: bool) -> tuple[dt.datetime, str]:
    quality = "OK" if clock_synced else "ESTIMATED"
    at: dt.datetime | None = None
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
        at = dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
    elif isinstance(value, str):
        try:
            at = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if at.tzinfo is None:
                at = None
        except ValueError:
            at = None
    # Passé d'un jour au plus, futur de 5 minutes au plus (la tolérance du cloud) : au-delà, datée à la réception. Un horodatage
    # en avance figerait le SoC local plusieurs heures (audit B18-2).
    if at is None or (received - at).total_seconds() > 86_400 or (at - received).total_seconds() > 300:
        return received, "ESTIMATED"
    return at.astimezone(dt.timezone.utc), quality


def _number(data: dict, key: str) -> float | None:
    v = data.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def _readings(
    source: str,
    ref: int | None,
    values: dict[str, float | None],
    at: dt.datetime,
    quality: str,
) -> list[Reading]:
    out = []
    for key, v in values.items():
        if v is None:
            continue
        lo, hi, _ = RANGES[key]
        if not lo <= v <= hi:
            raise ParseError(f"{key} hors plage physique ({v})")
        out.append(Reading(source, ref, key, v, at, quality))
    if not out:
        raise ParseError("aucune grandeur reconnue")
    return out


#: Composants Shelly porteurs d'une mesure : prise commutée, compteur d'énergie, compteur monophasé.
COMPOSANTS_SHELLY = ("switch:0", "em:0", "pm1:0")


def _grandeurs_shelly(data: dict) -> dict[str, float | None]:
    """Les cinq grandeurs d'un composant Shelly Gen2, quelle que soit la façon dont il a été publié."""
    energy = data.get("aenergy") if isinstance(data.get("aenergy"), dict) else {}
    return {
        "power_w": _number(data, "apower") if "apower" in data else _number(data, "act_power"),
        "voltage_v": _number(data, "voltage"),
        "current_a": _number(data, "current"),
        "energy_total_wh": _number(energy, "total") if energy else None,
        "power_factor": _number(data, "pf"),
    }


def shelly_gen2(device_id: int, payload: bytes, received: dt.datetime, clock_synced: bool = True) -> list[Reading]:
    """Statut natif Shelly Gen2 (``<id>/status/switch:0``, ``em:0``, ``pm1:0``) : apower, voltage, current, aenergy.total."""
    data = _json(payload)
    # ``aenergy.minute_ts`` date le début de la dernière minute du compteur, pas la puissance instantanée : un statut Shelly
    # est publié au changement, il est daté à sa réception (précision < 1 s sur horloge synchronisée, CA-18.5).
    at, quality = received, ("OK" if clock_synced else "ESTIMATED")
    return _readings("device", device_id, _grandeurs_shelly(data), at, quality)


def shelly_gen2_rpc(device_id: int, payload: bytes, received: dt.datetime, clock_synced: bool = True) -> list[Reading]:
    """Notification RPC d'un Shelly Gen2 (``<id>/events/rpc``) : ``NotifyStatus`` / ``NotifyFullStatus``.

    **C'est ce sujet-là qu'une prise sortie d'usine publie.** Les sujets ``<id>/status/<composant>`` ne sont
    alimentés que si l'option « Generic status update over MQTT » est cochée sur la prise — elle est
    DÉSACTIVÉE par défaut sur le micrologiciel Gen2. Un agent qui n'écoute que ``/status/...`` ne reçoit donc
    rien d'une prise qu'on vient de brancher, sans qu'aucune erreur ne le dise : ni message, ni rejet, ni
    relevé. Écouter aussi ``/events/rpc`` rend la pose d'une prise réelle indépendante d'un réglage que
    personne ne pense à faire — et à faire huit fois.

    Deux publications simultanées ne comptent pas double : ``local_measurements`` porte une contrainte
    d'unicité et l'insertion est en ``DO NOTHING``.

    Une notification sans mesure — un changement de ``sys``, de ``cloud``, ou un ``NotifyEvent`` de bouton —
    rend une liste vide. Ce n'est pas une erreur : c'est le trafic normal de ce sujet, et le signaler
    noierait les vrais rejets dans l'avertissement.
    """
    data = _json(payload)
    if data.get("method") not in ("NotifyStatus", "NotifyFullStatus"):
        return []
    params = data.get("params")
    if not isinstance(params, dict):
        return []
    at, quality = received, ("OK" if clock_synced else "ESTIMATED")
    for composant in COMPOSANTS_SHELLY:
        bloc = params.get(composant)
        if isinstance(bloc, dict):
            grandeurs = _grandeurs_shelly(bloc)
            # Un composant peut n'annoncer que son état (``output``) : pas de mesure, donc rien à écrire.
            if any(v is not None for v in grandeurs.values()):
                return _readings("device", device_id, grandeurs, at, quality)
    return []


def h2s_device(device_id: int, payload: bytes, received: dt.datetime, clock_synced: bool = True) -> list[Reading]:
    """Format JSON H2S d'une passerelle : power_w, current_a, voltage_v, energy_wh, power_factor. PowerLogic et Linky se lisent aussi
    directement (``modbus``, ``tic``, E172)."""
    data = _json(payload)
    at, quality = _when(data.get("timestamp"), received, clock_synced)
    values = {
        k: _number(data, k)
        for k in (
            "power_w",
            "current_a",
            "voltage_v",
            "energy_wh",
            "energy_total_wh",
            "power_factor",
        )
    }
    return _readings("device", device_id, values, at, quality)


def h2s_battery(stock_id: int, payload: bytes, received: dt.datetime, clock_synced: bool = True) -> list[Reading]:
    """État batterie au format H2S (``/battery/status``) : soc, puissances de charge et décharge, tension, température."""
    data = _json(payload)
    at, quality = _when(data.get("timestamp"), received, clock_synced)
    values = {
        "soc_pct": _number(data, "soc_pct") if "soc_pct" in data else _number(data, "soc"),
        "power_charge_w": _number(data, "power_charge_w") if "power_charge_w" in data else (_number(data, "charge_power_w") if "charge_power_w" in data else _number(data, "power_charge")),
        "power_discharge_w": _number(data, "power_discharge_w") if "power_discharge_w" in data else (_number(data, "discharge_power_w") if "discharge_power_w" in data else _number(data, "power_discharge")),
        "voltage_v": _number(data, "voltage_v") if "voltage_v" in data else _number(data, "voltage"),
        "current_a": _number(data, "current_a") if "current_a" in data else _number(data, "current"),
        "temperature_c": _number(data, "temperature_c") if "temperature_c" in data else _number(data, "temperature"),
    }
    return _readings("battery", stock_id, values, at, quality)


def h2s_weather(payload: bytes, received: dt.datetime, clock_synced: bool = True) -> list[Reading]:
    data = _json(payload)
    at, quality = _when(data.get("timestamp"), received, clock_synced)
    values = {
        k: _number(data, k)
        for k in (
            "temperature_c",
            "irradiance_w_m2",
            "wind_speed_m_s",
            "humidity_pct",
            "rain_mm",
        )
    }
    return _readings("weather", None, values, at, quality)
