"""Client du cloud H2S (§5.5, D249). Chaque appel est signé ; aucune dépendance au-delà de la bibliothèque standard.

Deux familles d'échec, que l'orchestrateur traite différemment :
* ``CloudUnavailable`` — réseau, délai, 5xx, 429 : la donnée reste dans le buffer, la perte de réseau est comptée ;
* ``CloudRefused`` — 4xx : la requête est fausse ou l'agent n'est plus reconnu (401 : révoqué ou horloge décalée).
"""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import tls
from .clock import Clock, parse_instant, utcnow
from .crypto import KeyStore, signed_headers


class CloudUnavailable(Exception):
    pass


class CloudServerError(CloudUnavailable):
    """HTTP 500 : le cloud répond mais ne sait pas traiter la requête. Ce n'est pas une perte de réseau (relecture B18-1)."""


class CloudRefused(Exception):
    def __init__(self, status: int, code: str | None, message: str) -> None:
        super().__init__(f"{status} {code or ''} {message}".strip())
        self.status = status
        self.code = code


@dataclass
class Response:
    status: int
    body: Any
    latency_ms: int


class CloudClient:
    def __init__(
        self,
        base_url: str,
        keys: KeyStore,
        clock: Clock,
        timeout_s: float = 10.0,
        ca_file: str | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._keys = keys
        self._clock = clock
        self._timeout = timeout_s
        self._agent: str | None = None
        # TLS 1.3 au minimum (CDC §3.3, C-2) : un proxy resté en TLS 1.2 est refusé à la poignée de main, jamais rattrapé.
        self._ssl = tls.client_context(ca_file) if base_url.startswith("https://") else None
        self.last_latency_ms: int | None = None

    def identify(self, agent_uuid: str) -> None:
        self._agent = agent_uuid

    def _call(self, method: str, path: str, body: Any = None, signed: bool = True) -> Response:
        data = b"" if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "h2s-agent",
        }
        if signed:
            if self._agent is None:
                raise CloudRefused(401, "edge.not_enrolled", "agent non enrôlé")
            headers.update(
                signed_headers(
                    self._keys,
                    self._agent,
                    method,
                    path,
                    int(self._clock.now().timestamp()),
                    data,
                )
            )
        request = urllib.request.Request(
            self._base + path,
            data=data if method != "GET" else None,
            method=method,
            headers=headers,
        )
        sent_at = utcnow()
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self._timeout, context=self._ssl) as r:
                raw = r.read(4 * 1024 * 1024 + 1)
                status = r.status
        except urllib.error.HTTPError as e:
            raw = e.read(65_536)
            status = e.code
            self._observe_header(e.headers, sent_at)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise CloudUnavailable(str(e)) from e
        latency = int((time.monotonic() - started) * 1000)
        self.last_latency_ms = latency
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        if status == 500:
            raise CloudServerError("HTTP 500")
        if status > 500 or status == 429:
            raise CloudUnavailable(f"HTTP {status}")
        if status >= 400:
            code = parsed.get("code") if isinstance(parsed, dict) else None
            detail = parsed.get("detail") if isinstance(parsed, dict) else ""
            raise CloudRefused(status, code, str(detail or ""))
        if isinstance(parsed, dict) and isinstance(parsed.get("server_time"), str):
            try:
                server = parse_instant(parsed["server_time"])
                self._clock.observe_server_time(server, sent_at, utcnow())
            except ValueError:
                pass
        return Response(status, parsed, latency)

    def _observe_header(self, headers: Any, sent_at: dt.datetime) -> None:
        """Heure du serveur sur un refus (401 d'une horloge décalée) : seul moyen de se recaler, digne de foi sous TLS (B18-7)."""
        raw = headers.get("X-H2S-Server-Time") if headers is not None else None
        if (
            raw
            and str(raw).isdigit()
            and (self._base.startswith("https://") or "://127.0.0.1" in self._base or "://localhost" in self._base)
        ):
            self._clock.observe_server_time(
                dt.datetime.fromtimestamp(int(raw), tz=dt.timezone.utc),
                sent_at,
                utcnow(),
            )

    # ------------------------------------------------------------------ routes

    def register(
        self,
        code: str,
        agent_uuid: str,
        agent_version: str,
        attestation: dict | None = None,
    ) -> dict:
        body = {
            "code": code,
            "agent_uuid": agent_uuid,
            "public_key": self._keys.public_key(),
            "key_store": self._keys.kind,
            "key_algorithm": getattr(self._keys, "algorithm", "Ed25519"),
            "agent_version": agent_version,
        }
        if attestation is not None:
            body["attestation"] = attestation
        return self._call("POST", "/api/agent/register", body, signed=False).body

    def attest(self, secret_b64: str) -> dict:
        """Secret du défi d'activation rendu par le TPM (E187), dans une requête signée par la clé TPM."""
        return self._call("POST", "/api/agent/attest", {"secret": secret_b64}).body

    def heartbeat(
        self,
        state: str,
        version: str,
        buffer_size: int,
        manual_mode: bool = False,
        tpm: dict | None = None,
        command_key: str | None = None,
    ) -> dict:
        body = {
            "state": state,
            "agent_version": version,
            "buffer_size": buffer_size,
            "clock_synced": self._clock.synced,
            "manual_mode": manual_mode,
        }
        if command_key is not None:
            # Clé X25519 publique : le cloud chiffre pour l'agent seul le secret d'une commande (mot de passe Wi-Fi, E020).
            body["command_public_key"] = command_key
        if tpm is not None:
            body["tpm"] = {
                k: tpm.get(k)
                for k in (
                    "status",
                    "simulator",
                    "manufacturer",
                    "firmware",
                    "ek_certificate",
                )
            }
        return self._call("POST", "/api/agent/heartbeat", body).body

    def sync(self, records: list[dict], path: str = "/api/agent/sync/buffer") -> dict:
        return self._call("POST", path, {"records": records}).body

    def gradients(self, body: dict) -> dict:
        return self._call("POST", "/api/agent/sync/gradients", body).body

    def decisions_current(self) -> dict:
        return self._call("GET", "/api/agent/decisions/current").body

    def decisions_batch(self, since: str | None) -> dict:
        path = "/api/agent/decisions/batch"
        if since:
            path += "?since=" + urllib.request.quote(since, safe="")
        return self._call("GET", path).body

    def commands(self) -> dict:
        """Commandes d'équipement signées en attente (E010, E011, E020)."""
        return self._call("GET", "/api/agent/commands").body

    def command_result(self, command_id: str, body: dict) -> dict:
        return self._call(
            "POST",
            "/api/agent/commands/" + urllib.request.quote(command_id, safe="") + "/result",
            body,
        ).body

    def rules(self) -> dict:
        return self._call("GET", "/api/agent/rules").body

    def config(self) -> dict:
        return self._call("GET", "/api/agent/config").body

    def model(self, model_type: str) -> dict:
        return self._call(
            "GET",
            "/api/agent/models/current?type=" + urllib.request.quote(model_type, safe=""),
        ).body
