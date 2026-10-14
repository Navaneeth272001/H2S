"""Fixtures : base PostgreSQL de test réelle (H2S_EDGE_TEST_DB_URL), horloge fixe, cloud et broker simulés."""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import uuid

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from h2s_agent.clock import FixedClock
from h2s_agent.cloud import CloudRefused, CloudUnavailable
from h2s_agent.crypto import b64url
from h2s_agent.decisions import Identity
from h2s_agent.storage import Database, migrate

DB_URL = os.environ.get("H2S_EDGE_TEST_DB_URL")
T0 = dt.datetime(2026, 9, 14, 8, 0, tzinfo=dt.timezone.utc)
SITE = 4


def pytest_collection_modifyitems(config, items):
    if DB_URL:
        return
    skip = pytest.mark.skip(reason="H2S_EDGE_TEST_DB_URL absente : tests sur PostgreSQL ignorés")
    for item in items:
        if "db" in getattr(item, "fixturenames", ()):
            item.add_marker(skip)


@pytest.fixture
def db():
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS public CASCADE")
        c.execute("CREATE SCHEMA public")
    database = Database(DB_URL)
    migrate(database)
    yield database
    database.close()


@pytest.fixture
def clock():
    return FixedClock(T0)


class CloudKeys:
    """Clés du « cloud » de test : signature Ed25519 des ordres et modèles, RSA pour les contributions."""

    def __init__(self) -> None:
        self.signing = ed25519.Ed25519PrivateKey.generate()
        self.public = b64url(
            self.signing.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )
        self.rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.rsa_public = base64.b64encode(
            self.rsa.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).decode()
        self.key_id = "testkey000000001"

    def sign(self, payload: dict | str) -> dict:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return {
            "payload": text,
            "signature": b64url(self.signing.sign(text.encode())),
            "key_id": self.key_id,
        }

    def order(
        self,
        now: dt.datetime,
        action: str = "CHARGE",
        power: float = 400,
        duration: int = 3600,
        stock: int = 1,
        site: int = SITE,
        validity: dt.timedelta = dt.timedelta(hours=24),
    ) -> dict:
        return self.sign(
            {
                "v": 1,
                "type": "decision",
                "decision_id": str(uuid.uuid4()),
                "site_id": site,
                "stock_id": stock,
                "action": action,
                "power_w": f"{power:.2f}",
                "duration_s": duration,
                "priority": 2,
                "origin": "AUTO",
                "valid_from": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + validity).isoformat().replace("+00:00", "Z"),
            }
        )


@pytest.fixture
def cloud_keys():
    return CloudKeys()


@pytest.fixture
def identity(cloud_keys):
    return Identity(
        str(uuid.uuid4()),
        SITE,
        cloud_keys.key_id,
        cloud_keys.public,
        cloud_keys.rsa_public,
    )


class FakePublisher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.retained: dict[str, str] = {}
        self.down = False

    def publish(self, topic: str, payload: str, qos: int = 1, retain: bool = False) -> bool:
        if self.down:
            return False
        self.sent.append((topic, json.loads(payload) if payload.startswith("{") else payload))
        if retain:
            self.retained[topic] = payload
        return True


@pytest.fixture
def publisher():
    return FakePublisher()


class FakeCloud:
    """Cloud en mémoire, au contrat de ``CloudClient`` : injoignable à la demande, accusés de réception par identifiant."""

    def __init__(self, keys: CloudKeys) -> None:
        self.keys = keys
        self.down = False
        self.refuse: int | None = None
        self.orders: list[dict] = []
        self.received: list[dict] = []
        self.gradient_bodies: list[dict] = []
        self.heartbeats = 0
        self.last_latency_ms = 3
        self.rules_body = {
            "site_id": SITE,
            "stocks": [
                {
                    "stock_id": 1,
                    "name": "Jackery",
                    "soc_min": 15,
                    "soc_max": 95,
                    "p_charge_max_w": 500,
                    "p_discharge_max_w": 1000,
                    "min_transition_interval_s": 30,
                    "capacity_wh": 1002,
                }
            ],
        }
        self.model_body: dict | None = None
        self.device_commands: list[dict] = []
        self.command_results: list[tuple[str, dict]] = []

        class _K:
            kind = "SOFTWARE"

        self._keys = _K()

    def _check(self) -> None:
        if self.down:
            raise CloudUnavailable("réseau coupé")
        if self.refuse:
            raise CloudRefused(self.refuse, "edge.unauthorized", "refus")

    def identify(self, agent_uuid: str) -> None:
        pass

    def heartbeat(
        self,
        state: str,
        version: str,
        buffer_size: int,
        manual_mode: bool = False,
        tpm: dict | None = None,
        command_key: str | None = None,
    ) -> dict:
        self._check()
        self.heartbeats += 1
        self.last_tpm = tpm
        self.last_command_key = command_key
        return {"server_time": "2026-09-14T08:00:00Z"}

    def sync(self, records: list[dict], path: str = "") -> dict:
        self._check()
        if any(r["payload"].get("later") for r in records):
            self._check()
            self.received.extend(r for r in records if not r["payload"].get("later"))
            return {
                "results": [
                    {
                        "id": r["id"],
                        "outcome": "DEFERRED" if r["payload"].get("later") else "ACCEPTED",
                        "reason": None,
                    }
                    for r in records
                ]
            }
        if any(r["payload"].get("boom") for r in records):
            from h2s_agent.cloud import CloudServerError

            raise CloudServerError("HTTP 500")
        self.received.extend(records)
        return {"results": [{"id": r["id"], "outcome": "ACCEPTED", "reason": None} for r in records]}

    def gradients(self, body: dict) -> dict:
        self._check()
        self.gradient_bodies.append(body)
        return {"message_id": body["message_id"], "outcome": "ACCEPTED"}

    def decisions_current(self) -> dict:
        self._check()
        return {"decisions": self.orders}

    def decisions_batch(self, since):
        self._check()
        return {"decisions": self.orders, "cursor": getattr(self, "batch_cursor", None)}

    def rules(self) -> dict:
        self._check()
        return self.rules_body

    def commands(self) -> dict:
        self._check()
        return {"commands": self.device_commands}

    def command_result(self, command_id: str, body: dict) -> dict:
        self._check()
        self.command_results.append((command_id, body))
        return {"status": body["status"]}

    def model(self, model_type: str) -> dict:
        self._check()
        return self.model_body or {}


@pytest.fixture
def fake_cloud(cloud_keys):
    return FakeCloud(cloud_keys)
