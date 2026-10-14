"""API locale de diagnostic (§5.4), sur 127.0.0.1 seulement, jeton porteur lu dans un fichier 0600.

``GET /api/agent/health`` est la seule route sans jeton (spécification). Le jeton n'est jamais journalisé ; la
comparaison est à temps constant. Un technicien sur le boîtier l'utilise ; rien n'est exposé au réseau du site.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

LOG = logging.getLogger("h2s_agent.local_api")
MAX_BODY = 16_384


def ensure_token(path: Path) -> str:
    import os

    if path.exists():
        return path.read_text(encoding="ascii").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(token)
    return token


class LocalApi:
    """Routes : chaque gestionnaire reçoit le corps JSON (ou None) et renvoie (statut, objet)."""

    def __init__(
        self,
        port: int,
        token: str,
        routes: dict[tuple[str, str], Callable[[Any], tuple[int, Any]]],
    ) -> None:
        self._token = token
        self._routes = routes
        api = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "h2s-agent"

            def log_message(self, fmt: str, *args: Any) -> None:  # pas d'en-têtes (jeton) dans les journaux
                LOG.debug("%s %s", self.command, self.path.split("?")[0])

            def _send(self, status: int, body: Any) -> None:
                raw = json.dumps(body, default=str).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            timeout = 10  # une connexion lente ou muette ne tient pas un fil indéfiniment

            def _handle(self, method: str) -> None:
                path = self.path.split("?")[0]
                route = api._routes.get((method, path))
                if route is None:
                    self._send(404, {"code": "not_found"})
                    return
                if path != "/api/agent/health":
                    auth = self.headers.get("Authorization", "")
                    if not auth.startswith("Bearer ") or not hmac.compare_digest(
                        auth[7:].encode(), api._token.encode()
                    ):
                        self._send(401, {"code": "unauthorized"})
                        return
                body = None
                raw_length = (self.headers.get("Content-Length") or "0").strip()
                if not raw_length.isdigit():
                    # Longueur négative ou illisible : rfile.read(-1) lirait jusqu'à la fin de la connexion (audit B18-12).
                    self._send(400, {"code": "invalid_length"})
                    return
                length = int(raw_length)
                if length > MAX_BODY:
                    self._send(413, {"code": "payload_too_large"})
                    return
                if length:
                    try:
                        body = json.loads(self.rfile.read(length).decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        self._send(400, {"code": "invalid_json"})
                        return
                try:
                    status, out = route(body)
                except (ValueError, KeyError, TypeError) as exc:
                    status, out = (
                        400,
                        {"code": "invalid_request", "detail": str(exc)[:200]},
                    )
                except Exception:
                    LOG.exception("erreur de l'API locale")
                    status, out = 500, {"code": "internal_error"}
                self._send(status, out)

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

            def do_PUT(self) -> None:
                self._handle("PUT")

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="h2s-local-api", daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
