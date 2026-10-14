"""Pilotage indirect de batterie physique par relais Shelly Gen2 (spécification Brique 18 / Chemin B).

Quand la batterie physique (ex: Jackery) alimente les appareils à travers des prises Shelly EM sans interface directe:
- DISCHARGE : allumer les prises Shelly des équipements prioritaires (décharge physique de la batterie).
- CHARGE    : couper les prises Shelly des équipements non critiques (réduction de charge simulant la charge par surplus).
- STANDBY   : maintenir l'état actuel des relais.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Sequence

LOG = logging.getLogger("h2s_agent.shelly_relay")


def set_relay(ip: str, on: bool, timeout_s: float = 3.0, http_port: int = 80) -> bool:
    """Envoie une commande JSON-RPC Switch.Set à un Shelly Gen2."""
    url = f"http://{ip}:{http_port}/rpc/Switch.Set"
    body = json.dumps({"id": 0, "on": on}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        LOG.warning("impossible d'actionner le relais Shelly sur %s : %s", ip, exc)
        return False


def apply_battery_decision(
    action: str,
    priority_ips: Sequence[str],
    non_critical_ips: Sequence[str],
    timeout_s: float = 3.0
) -> dict:
    """Applique la décision énergétique sur les relais physiques Shelly (Chemin B)."""
    norm = action.upper().strip()
    result = {"action": norm, "switched": [], "errors": []}

    if norm == "DISCHARGE":
        for ip in priority_ips:
            ok = set_relay(ip, True, timeout_s=timeout_s)
            if ok:
                result["switched"].append({"ip": ip, "state": True})
            else:
                result["errors"].append({"ip": ip, "state": True})

    elif norm == "CHARGE":
        for ip in non_critical_ips:
            ok = set_relay(ip, False, timeout_s=timeout_s)
            if ok:
                result["switched"].append({"ip": ip, "state": False})
            else:
                result["errors"].append({"ip": ip, "state": False})

    return result
