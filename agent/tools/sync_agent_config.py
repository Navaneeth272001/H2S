#!/usr/bin/env python3
"""Safely update /etc/h2s-agent/agent.json without modifying existing credentials.

Preserves all existing identity/security/broker settings (cloud_url, db_url,
data_dir, key_store, tpm_tcti, require_tpm, package_public_key, mqtt,
cloud_mqtt, local_api_port) and ensures:
- Consumption devices (8..15) are present
- Production devices (20=SOLAR, 21=WIND, plus any PRODUCTION device_ids known
  from the cloud's policy.site in local PostgreSQL) are present on
  h2s/devices/<id>/status
- Theoretical renewable sources (solar + wind) are present in "sources"
- Battery (stock_id=1 on h2s/site4/battery/status) is present
- weather_topic is set to h2s/site4/weather/status
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_CONSUMPTION_IDS = (8, 9, 10, 11, 12, 13, 14, 15)
DEFAULT_PRODUCTION_IDS = (20, 21)
DEFAULT_SOURCES = [
    {
        "source_id": 20,
        "type": "solar",
        "surface_m2": 10.0,
        "rendement_pv": 0.22,
    },
    {
        "source_id": 21,
        "type": "wind",
        "diametre_rotor_m": 3.0,
        "rendement_eolien": 0.35,
        "vent_demarrage_m_s": 3.0,
        "vent_arret_m_s": 25.0,
        "puissance_nominale_w": 2000.0,
    },
]


def _discover_cloud_roles(db_url: str) -> dict[int, str]:
    """Read device roles synced from cloud (/api/agent/rules -> agent_kv.policy.site) if available."""
    if not db_url:
        return {}
    try:
        import psycopg
        with psycopg.connect(db_url, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM agent_kv WHERE key = 'policy.site'")
                row = cur.fetchone()
                if not row or not row[0]:
                    return {}
                policy = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                roles = (policy or {}).get("roles") or {}
                out: dict[int, str] = {}
                for k, role in roles.items():
                    if isinstance(k, str) and k.startswith("device:"):
                        dev_str = k.split(":", 1)[1]
                        if dev_str.isdigit() and role in ("CONSUMPTION", "PRODUCTION"):
                            out[int(dev_str)] = role
                return out
    except Exception:
        return {}


def update_agent_json(config_path: Path) -> dict:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    cloud_roles = _discover_cloud_roles(raw.get("db_url", ""))

    existing_devices = list(raw.get("devices") or [])
    by_id: dict[int, dict] = {}
    for d in existing_devices:
        if isinstance(d, dict) and d.get("device_id") is not None:
            dev_id = int(d["device_id"])
            by_id[dev_id] = dict(d)

    for dev_id in DEFAULT_CONSUMPTION_IDS:
        role = cloud_roles.get(dev_id, "CONSUMPTION")
        if dev_id not in by_id:
            by_id[dev_id] = {
                "device_id": dev_id,
                "kind": "h2s_json",
                "topic": f"h2s/devices/{dev_id}/status",
                "role": role,
            }
        else:
            by_id[dev_id].setdefault("kind", "h2s_json")
            by_id[dev_id].setdefault("topic", f"h2s/devices/{dev_id}/status")
            by_id[dev_id]["role"] = cloud_roles.get(dev_id, by_id[dev_id].get("role", "CONSUMPTION"))

    prod_ids = set(DEFAULT_PRODUCTION_IDS) | {
        dev_id for dev_id, role in cloud_roles.items() if role == "PRODUCTION"
    }
    for dev_id in sorted(prod_ids):
        if dev_id not in by_id:
            by_id[dev_id] = {
                "device_id": dev_id,
                "kind": "h2s_json",
                "topic": f"h2s/devices/{dev_id}/status",
                "role": "PRODUCTION",
            }
        else:
            by_id[dev_id]["role"] = "PRODUCTION"
            if by_id[dev_id].get("kind") == "h2s_json":
                by_id[dev_id].setdefault("topic", f"h2s/devices/{dev_id}/status")

    raw["devices"] = [by_id[k] for k in sorted(by_id)]

    if not raw.get("batteries"):
        raw["batteries"] = [
            {
                "stock_id": 1,
                "status_topic": "h2s/site4/battery/status",
                "command_prefix": "h2s/site4/commands",
            }
        ]

    if not raw.get("sources"):
        raw["sources"] = DEFAULT_SOURCES

    if not raw.get("weather_topic"):
        raw["weather_topic"] = "h2s/site4/weather/status"

    config_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="/etc/h2s-agent/agent.json",
        help="Path to agent.json (default: /etc/h2s-agent/agent.json)",
    )
    args = parser.parse_args()
    path = Path(args.config)
    if not path.exists():
        print(f"Config file not found: {path}", file=sys.stderr)
        return 1
    updated = update_agent_json(path)
    prod = [d["device_id"] for d in updated.get("devices", []) if d.get("role") == "PRODUCTION"]
    cons = [d["device_id"] for d in updated.get("devices", []) if d.get("role") == "CONSUMPTION"]
    print(
        f"Updated {path}: consumption={cons}, production={prod}, "
        f"batteries={[b['stock_id'] for b in updated.get('batteries', [])]}, "
        f"weather_topic={updated.get('weather_topic')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
