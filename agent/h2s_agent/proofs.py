"""Preuves locales d'effacement (§8 B11, E188) : relevé signé par période, produit même hors réseau.

Toutes les 30 minutes closes (grâce de 2 minutes), l'agent établit ce qu'il a **mesuré et exécuté** sur la période :

* énergie consommée par équipement (accroissements du compteur cumulé, remise à zéro comprise), puissance moyenne ;
* SoC de début et de fin de chaque batterie ; ordres exécutés, refusés ou en échec (source : direct, cache, manuel, veille) ;
* nombre et empreinte SHA-256 de tous les relevés bruts de la période (le cloud peut rapprocher les mesures remontées) ;
* ``offline`` : l'agent a été en mode dégradé pendant la période (preuve établie sans le cloud).

La charge utile est signée par la clé d'identité (``KeyStore`` : Ed25519 logicielle, ou ECDSA P-256 dans le TPM) et chaînée à
la précédente (``prev_hash``). Elle part dans le buffer en priorité 1 (jamais évincée), est gardée en base locale, et le cloud
la vérifie avec la clé enrôlée à la resynchronisation. Le calcul de l'effacement (écart à la courbe de référence) reste au cloud
(brique 11) : l'agent ne produit que des faits signés, jamais une estimation.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Callable

from .buffer import Buffer
from .clock import utcnow
from .crypto import KeyStore, sha256_hex
from .storage import Database

PERIOD = dt.timedelta(minutes=30)
GRACE = dt.timedelta(minutes=2)
#: Rattrapage au plus : la rétention du buffer (7 jours de coupure tiennent, D251).
BACKLOG = dt.timedelta(days=7)
COUNTER_LOOKBACK = dt.timedelta(hours=1)


def _floor(at: dt.datetime) -> dt.datetime:
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    return epoch + ((at - epoch) // PERIOD) * PERIOD


def _iso(at: dt.datetime) -> str:
    return at.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


class Proofs:
    def __init__(
        self,
        db: Database,
        buffer: Buffer,
        keys: KeyStore,
        identity: Callable[[], object | None],
        now: Callable[[], dt.datetime],
    ) -> None:
        self._db = db
        self._buffer = buffer
        self._keys = keys
        self._identity = identity
        self._now = now

    def build(self, now: dt.datetime | None = None) -> int:
        """Établit les preuves des périodes closes non encore prouvées ; renvoie leur nombre."""
        ident = self._identity()
        if ident is None:
            return 0
        now = now or self._now()
        end = _floor(now - GRACE)
        with self._db.tx() as cur:
            cur.execute("SELECT period_end, proof_hash FROM local_proofs ORDER BY period_start DESC LIMIT 1")
            last = cur.fetchone()
        if last is not None:
            start, prev = last["period_end"], last["proof_hash"]
        else:
            start, prev = end - PERIOD, None
        start = max(start, end - BACKLOG)
        built = 0
        while start + PERIOD <= end:
            prev = self._prove(ident, start, start + PERIOD, prev, now)
            start += PERIOD
            built += 1
        return built

    def _prove(
        self,
        ident,
        start: dt.datetime,
        end: dt.datetime,
        prev: str | None,
        now: dt.datetime,
    ) -> str:
        statement = self.statement(ident, start, end, prev, now)
        payload = json.dumps(statement, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        signature = self._keys.sign(payload.encode("utf-8"))
        digest = sha256_hex(payload.encode("utf-8"))
        with self._db.tx() as cur:
            # Preuve et enregistrement du buffer dans la même transaction : jamais une preuve gardée sans envoi, ni l'inverse.
            cur.execute(
                "INSERT INTO local_proofs (period_start, period_end, payload, signature, key_algorithm, proof_hash, prev_hash, offline, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    start,
                    end,
                    payload,
                    signature,
                    statement["key_algorithm"],
                    digest,
                    prev,
                    statement["offline"],
                    now,
                ),
            )
            self._buffer.push_in(
                cur,
                "LOCAL_PROOF",
                {"payload": payload, "signature": signature, "hash": digest},
                end,
                now,
                priority=1,
            )
        return digest

    def statement(
        self,
        ident,
        start: dt.datetime,
        end: dt.datetime,
        prev: str | None,
        now: dt.datetime,
    ) -> dict:
        with self._db.tx() as cur:
            cur.execute(
                "SELECT device_id, device_type, data_type, value, measurement_timestamp AS at, data_quality FROM local_measurements"
                " WHERE device_type IN ('device', 'battery') AND measurement_timestamp >= %s AND measurement_timestamp < %s"
                " ORDER BY device_id, data_type, measurement_timestamp",
                (start, end),
            )
            rows = cur.fetchall()
            cur.execute(
                "SELECT device_id, value FROM local_measurements WHERE device_type = 'device' AND data_type = 'energy_total_wh'"
                " AND measurement_timestamp >= %s AND measurement_timestamp < %s ORDER BY device_id, measurement_timestamp DESC",
                (start - COUNTER_LOOKBACK, start),
            )
            before: dict[str, float] = {}
            for r in cur.fetchall():
                before.setdefault(r["device_id"], float(r["value"]))
            cur.execute(
                "SELECT decision_ref, stock_id, action, power_w, source, execution_status, executed_at FROM local_decisions"
                " WHERE executed_at >= %s AND executed_at < %s ORDER BY executed_at, decision_id",
                (start, end),
            )
            decisions = cur.fetchall()
            cur.execute(
                "SELECT count(*) AS n FROM agent_status_history WHERE status = 'DEGRADED' AND started_at < %s AND (ended_at IS NULL OR ended_at > %s)",
                (end, start),
            )
            offline = int(cur.fetchone()["n"]) > 0
        digest = hashlib.sha256()
        devices: dict[str, dict] = {}
        batteries: dict[str, dict] = {}
        for r in rows:
            digest.update(
                f"{r['device_id']}|{r['data_type']}|{float(r['value'])!r}|{_iso(r['at'])}|{r['data_quality']}\n".encode()
            )
            value = float(r["value"])
            if r["device_type"] == "device":
                d = devices.setdefault(
                    r["device_id"],
                    {
                        "device": r["device_id"],
                        "samples": 0,
                        "energy_wh": None,
                        "_p": [],
                        "estimated": False,
                    },
                )
                d["estimated"] = d["estimated"] or r["data_quality"] != "OK"
                if r["data_type"] == "power_w":
                    d["_p"].append(value)
                    d["samples"] += 1
                elif r["data_type"] == "energy_total_wh":
                    previous = d.get("_last", before.get(r["device_id"]))
                    if previous is not None:
                        d["energy_wh"] = (d["energy_wh"] or 0.0) + (value - previous if value >= previous else value)
                    d["_last"] = value
            elif r["data_type"] == "soc_pct":
                b = batteries.setdefault(
                    r["device_id"],
                    {
                        "stock_id": int(r["device_id"].split(":", 1)[1]),
                        "soc_start": value,
                        "samples": 0,
                    },
                )
                b["soc_end"] = value
                b["samples"] += 1
        for d in devices.values():
            powers = d.pop("_p")
            d.pop("_last", None)
            d["power_avg_w"] = round(sum(powers) / len(powers), 3) if powers else None
            d["energy_wh"] = round(d["energy_wh"], 3) if d["energy_wh"] is not None else None
        return {
            "v": 1,
            "type": "local_proof",
            "agent_uuid": str(ident.agent_uuid),
            "site_id": int(ident.site_id),
            "period_start": _iso(start),
            "period_end": _iso(end),
            "offline": offline,
            "devices": sorted(devices.values(), key=lambda d: d["device"]),
            "batteries": sorted(batteries.values(), key=lambda b: b["stock_id"]),
            "decisions": [
                {
                    "decision_ref": x["decision_ref"],
                    "stock_id": x["stock_id"],
                    "action": x["action"],
                    "power_w": round(float(x["power_w"]), 2) if x["power_w"] is not None else None,
                    "source": x["source"],
                    "execution_status": x["execution_status"],
                    "executed_at": _iso(x["executed_at"]),
                }
                for x in decisions
            ],
            "measurements": {"count": len(rows), "sha256": digest.hexdigest()},
            "prev_hash": prev,
            "key_algorithm": getattr(self._keys, "algorithm", "Ed25519"),
            "created_at": _iso(now or utcnow()),
        }
