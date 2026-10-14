"""Test de boucle d'intégration de batterie physique (Chemin B et paquet de mesures)."""

from __future__ import annotations

import datetime as dt
import json

from h2s_agent import parsers
from h2s_agent import shelly_relay


def test_format_paquet_batterie_reconnu_par_parseur():
    """Vérifie que le format de télémétrie de la batterie est accepté par l'agent."""
    now = dt.datetime(2026, 10, 14, 12, 0, 0, tzinfo=dt.timezone.utc)
    payload = {
        "timestamp": "2026-10-14T12:00:00Z",
        "soc_pct": 84.5,
        "power_charge_w": 0.0,
        "power_discharge_w": 320.0,
        "voltage_v": 230.2,
        "current_a": 1.39,
        "temperature_c": 23.5,
    }
    raw = json.dumps(payload).encode("utf-8")
    readings = parsers.h2s_battery(stock_id=1, payload=raw, received=now)
    assert len(readings) == 6
    valeurs = {r.data_type: r.value for r in readings}
    assert valeurs["soc_pct"] == 84.5
    assert valeurs["power_discharge_w"] == 320.0
    assert valeurs["power_charge_w"] == 0.0
    assert valeurs["voltage_v"] == 230.2


def test_traduction_decisions_relais_shelly(monkeypatch):
    """Vérifie la bascule des relais Shelly pour CHARGE et DISCHARGE."""
    appels = []

    def mock_set_relay(ip, on, timeout_s=3.0, http_port=80):
        appels.append((ip, on))
        return True

    monkeypatch.setattr(shelly_relay, "set_relay", mock_set_relay)

    prioritaires = ["192.168.1.10", "192.168.1.197", "192.168.1.176"]
    non_critiques = ["192.168.1.103", "192.168.1.8", "192.168.1.130"]

    # 1. DISCHARGE : allume les prioritaires (la batterie alimente)
    res_dis = shelly_relay.apply_battery_decision("DISCHARGE", prioritaires, non_critiques)
    assert res_dis["action"] == "DISCHARGE"
    assert len(res_dis["switched"]) == 3
    assert all(a[1] is True for a in appels)

    # 2. CHARGE : éteint les non critiques (réduction de charge)
    appels.clear()
    res_chg = shelly_relay.apply_battery_decision("CHARGE", prioritaires, non_critiques)
    assert res_chg["action"] == "CHARGE"
    assert len(res_chg["switched"]) == 3
    assert all(a[1] is False for a in appels)
