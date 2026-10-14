"""Canal de commandes aux équipements (E010, E011, E018, E019, E020 ; spéc. 2 §4.3.2, §4.3.3) : test de connexion, recherche sur le
réseau local et configuration Wi-Fi d'une prise Shelly, exécutés par l'agent sur une prise simulée ; ordres signés par le cloud,
jamais hors du réseau privé du site, mot de passe chiffré pour l'agent et jamais gardé."""

from __future__ import annotations

import datetime as dt
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from h2s_agent import commands
from h2s_agent.clock import FixedClock

from .conftest import SITE, T0

VECTORS = Path(__file__).parent / "vectors.json"


class ShellySimulee:
    """Prise Shelly Gen2 (ou Gen1) sur 127.0.0.1 : /shelly, RPC WiFi.SetConfig, /settings/sta ; garde les appels reçus."""

    def __init__(self, gen: int = 2) -> None:
        self.calls: list[tuple[str, str, bytes]] = []
        self.closed = False
        sim = self

        class H(BaseHTTPRequestHandler):
            def _send(self, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                sim.calls.append(("GET", self.path, b""))
                if self.path == "/shelly":
                    self._send(
                        {
                            "id": "shellyplugsg3-aabbcc",
                            "mac": "AABBCCDDEEFF",
                            "model": "S3PL-00112EU",
                            "gen": 2,
                            "fw_id": "1.4.4",
                            "app": "PlugSG3",
                        }
                        if gen == 2
                        else {
                            "type": "SHPLG-S",
                            "mac": "AABBCCDDEEFF",
                            "auth": False,
                            "fw": "1.14.0",
                        }
                    )
                elif self.path.startswith("/settings/sta"):
                    self._send({"enabled": True, "ssid": "recette"})
                else:
                    self.send_error(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                sim.calls.append(("POST", self.path, body))
                if self.path == "/rpc/WiFi.SetConfig":
                    self._send({"restart_required": True})
                else:
                    self.send_error(404)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.server.shutdown()
            self.server.server_close()


@pytest.fixture
def shelly():
    s = ShellySimulee()
    yield s
    s.close()


def boucle_locale(ip) -> bool:
    """Banc de test : la prise simulée est sur la boucle locale ; en service, seul le réseau privé du site est admis."""
    return ip.is_loopback or commands.private_lan(ip)


def test_secret_de_commande_dechiffre_comme_le_cloud_le_chiffre(tmp_path):
    v = json.loads(VECTORS.read_text())["command_secret"]
    key = commands.CommandKey.from_private_bytes(bytes.fromhex(v["agent_private_hex"]))
    assert key.public_key() == v["agent_public"]
    assert commands.decrypt_secret(key, v["envelope"], v["command_id"]) == v["clear"]
    with pytest.raises(ValueError):
        (
            commands.decrypt_secret(key, v["envelope"], str(uuid.uuid4())),
            "données associées : lié à sa commande",
        )
    stored = commands.CommandKey.load_or_create(tmp_path / "keys" / "command-x25519.pem")
    assert (
        commands.CommandKey.load_or_create(tmp_path / "keys" / "command-x25519.pem").public_key() == stored.public_key()
    ), "clé stable"
    assert oct((tmp_path / "keys" / "command-x25519.pem").stat().st_mode & 0o777) == "0o600"


def test_ping_command_reports_reachability(shelly):
    ex = commands.Executor(http_port=shelly.port, allow=boucle_locale, timeout_s=2)
    r = ex.ping("127.0.0.1", "shelly_plug")
    assert (
        r["reachable"]
        and r["port"] == shelly.port
        and r["device"]["gen"] == 2
        and r["device"]["mac"] == "AA:BB:CC:DD:EE:FF"
    )
    assert r["latency_ms"] >= 0
    shelly.close()
    assert (
        commands.Executor(http_port=shelly.port, allow=boucle_locale, timeout_s=0.5).ping("127.0.0.1", "shelly_plug")[
            "reachable"
        ]
        is False
    )


def test_ping_hors_reseau_prive_refuse():
    with pytest.raises(commands.CommandRefused):
        commands.Executor().ping("8.8.8.8", "shelly_plug")
    with pytest.raises(commands.CommandRefused):
        (
            commands.Executor().ping("127.0.0.1", "shelly_plug"),
            "boucle locale : pas un équipement du réseau du site",
        )


def sans_mdns(timeout_s):
    return {"mode": None, "responders": 0, "services": [], "error": None}


def test_scan_finds_simulated_shelly(shelly):
    ex = commands.Executor(
        http_port=shelly.port,
        modbus_port=1,
        allow=boucle_locale,
        timeout_s=0.5,
        mdns=sans_mdns,
    )
    r = ex.scan("127.0.0.0/29")
    assert r["subnet"] == "127.0.0.0/29" and r["scanned"] == 6
    assert r["hosts"] == [
        {
            "ip": "127.0.0.1",
            "type": "shelly_plug",
            "name": "shellyplugsg3-aabbcc",
            "mac": "AA:BB:CC:DD:EE:FF",
            "model": "S3PL-00112EU",
            "gen": 2,
            "firmware": "1.4.4",
            "online": True,
            "discovery": ["http"],
        }
    ]


def test_scan_fusionne_la_decouverte_mdns(shelly):
    """E011, E019 : annonces mDNS (_shelly._tcp, _http._tcp) fusionnées avec le sondage du réseau, bornées au réseau recherché."""
    from h2s_agent import mdns

    asked = []

    def annonces(timeout_s):
        asked.append(timeout_s)
        return {
            "mode": "multicast",
            "responders": 3,
            "error": None,
            "services": [
                mdns.Service(
                    "shellyplugsg3-aabbcc._shelly._tcp.local",
                    "_shelly._tcp.local",
                    "127.0.0.1",
                    "shellyplugsg3-aabbcc.local",
                    shelly.port,
                    {"gen": "2", "app": "PlugSG3"},
                ),
                mdns.Service(
                    "shellyplus1pm-ddeeff._shelly._tcp.local",
                    "_shelly._tcp.local",
                    "127.0.0.2",
                    "shellyplus1pm-ddeeff.local",
                    80,
                    {"gen": "2", "app": "Plus1PM", "ver": "1.3.2"},
                ),
                mdns.Service(
                    "Onduleur._http._tcp.local",
                    "_http._tcp.local",
                    "127.0.0.3",
                    "onduleur.local",
                    80,
                    {},
                ),
                mdns.Service(
                    "shelly-autre-reseau._shelly._tcp.local",
                    "_shelly._tcp.local",
                    "10.9.9.9",
                    "x.local",
                    80,
                    {},
                ),
            ],
        }

    ex = commands.Executor(
        http_port=shelly.port,
        modbus_port=1,
        allow=boucle_locale,
        timeout_s=0.5,
        mdns=annonces,
        mdns_timeout_s=1.2,
    )
    r = ex.scan("127.0.0.0/29")
    assert asked == [1.2], "écoute mDNS bornée en durée"
    by_ip = {h["ip"]: h for h in r["hosts"]}
    assert set(by_ip) == {"127.0.0.1", "127.0.0.2", "127.0.0.3"}, "annonce hors du réseau recherché écartée"
    assert by_ip["127.0.0.1"]["discovery"] == ["http", "mdns"] and by_ip["127.0.0.1"]["mac"] == "AA:BB:CC:DD:EE:FF", (
        "une seule entrée par adresse"
    )
    assert by_ip["127.0.0.2"] == {
        "ip": "127.0.0.2",
        "type": "shelly_plug",
        "name": "shellyplus1pm-ddeeff",
        "mac": None,
        "model": "Plus1PM",
        "gen": 2,
        "firmware": "1.3.2",
        "online": True,
        "discovery": ["mdns"],
    }, "prise annoncée mais fiche /shelly injoignable : TXT"
    assert by_ip["127.0.0.3"]["type"] == "http_device" and by_ip["127.0.0.3"]["name"] == "Onduleur"
    assert r["mdns"] == {
        "mode": "multicast",
        "responders": 3,
        "announced": 4,
        "error": None,
    }
    assert [h["ip"] for h in r["hosts"]] == ["127.0.0.1", "127.0.0.2", "127.0.0.3"], "ordre des adresses"


def test_scan_sans_mdns_disponible_reste_un_sondage(shelly):
    def panne(timeout_s):
        raise OSError("interface sans multicast")

    r = commands.Executor(
        http_port=shelly.port,
        modbus_port=1,
        allow=boucle_locale,
        timeout_s=0.5,
        mdns=panne,
    ).scan("127.0.0.0/30")
    assert [h["ip"] for h in r["hosts"]] == ["127.0.0.1"]
    assert r["mdns"]["announced"] == 0 and "multicast" in r["mdns"]["error"]


def test_scan_refuses_public_range():
    with pytest.raises(commands.CommandRefused):
        commands.Executor().scan("8.8.8.0/24")
    with pytest.raises(commands.CommandRefused):
        commands.Executor().scan("10.0.0.0/16"), "au plus un /24 : débit borné"


def test_wifi_config_calls_shelly_rpc(shelly):
    ex = commands.Executor(http_port=shelly.port, allow=boucle_locale, timeout_s=2)
    r = ex.wifi("127.0.0.1", "recette-h2s", "motdepasse-8")
    assert r == {"applied": True, "gen": 2, "restart_required": True}
    method, path, body = shelly.calls[-1]
    assert (method, path) == ("POST", "/rpc/WiFi.SetConfig")
    assert json.loads(body) == {"config": {"sta": {"ssid": "recette-h2s", "pass": "motdepasse-8", "enable": True}}}


def test_wifi_shelly_gen1():
    s = ShellySimulee(gen=1)
    try:
        r = commands.Executor(http_port=s.port, allow=boucle_locale, timeout_s=2).wifi(
            "127.0.0.1", "recette", "motdepasse-8"
        )
        assert r["applied"] and r["gen"] == 1
        assert s.calls[-1][1].startswith("/settings/sta?") and "key=motdepasse-8" in s.calls[-1][1]
    finally:
        s.close()


def test_redirection_http_d_un_appareil_non_suivie(shelly):
    """S9 : une « prise » du réseau local qui redirige (3xx) vers une autre adresse n'est pas suivie — sinon un hôte du LAN
    ferait interroger au boîtier sa propre API locale ou toute autre adresse de la boucle."""
    cible = shelly

    class Redirige(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{cible.port}{self.path}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirige)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ex = commands.Executor(http_port=server.server_address[1], allow=boucle_locale, timeout_s=2)
        r = ex.ping("127.0.0.1", "shelly_plug")
        assert r["reachable"] and "device" not in r, r
        assert cible.calls == [], "la cible de la redirection n'est jamais appelée"
    finally:
        server.shutdown()
        server.server_close()


class Canal:
    """Transport en mémoire au contrat de ``CommandChannel`` (le sondage signé HTTP aujourd'hui, MQTT au lot 5)."""

    def __init__(self) -> None:
        self.pending: list[dict] = []
        self.results: list[tuple[str, dict]] = []

    def fetch(self) -> list[dict]:
        return list(self.pending)

    def report(self, command_id: str, body: dict) -> None:
        self.results.append((command_id, body))


def ordre(
    cloud_keys,
    kind: str,
    target: dict | None,
    params: dict | None = None,
    secret: dict | None = None,
    site: int = SITE,
    expires: dt.datetime | None = None,
    command_id: str | None = None,
) -> dict:
    return cloud_keys.sign(
        {
            "v": 1,
            "type": "device_command",
            "command_id": command_id or str(uuid.uuid4()),
            "site_id": site,
            "command": kind,
            "target": target,
            "params": params or {},
            "secret": secret,
            "expires_at": (expires or T0 + dt.timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
        }
    )


def test_commandes_signees_executees_une_fois_et_rendues(shelly, identity, cloud_keys, tmp_path):
    canal = Canal()
    alerts: list[str] = []
    key = commands.CommandKey.load_or_create(tmp_path / "command.pem")
    clock = FixedClock(T0)
    dc = commands.DeviceCommands(
        canal,
        lambda: identity,
        key,
        commands.Executor(http_port=shelly.port, allow=boucle_locale, timeout_s=1),
        clock.now,
        lambda code, sev, msg, **kw: alerts.append(code),
        inline=True,
    )
    ping = ordre(
        cloud_keys,
        "PING",
        {"device_id": 5, "ip": "127.0.0.1", "device_type": "shelly_plug"},
    )
    envelope = commands.encrypt_secret(key.public_key(), "motdepasse-8", "5b1d2c3e-4f50-4a6b-8c7d-9e0f1a2b3c4d")
    wifi = ordre(
        cloud_keys,
        "WIFI_CONFIG",
        {"device_id": 5, "ip": "127.0.0.1", "device_type": "shelly_plug"},
        {"ssid": "recette-h2s"},
        envelope,
        command_id="5b1d2c3e-4f50-4a6b-8c7d-9e0f1a2b3c4d",
    )
    forged = dict(ping, payload=ping["payload"].replace("127.0.0.1", "192.168.1.66"))
    foreign = ordre(
        cloud_keys,
        "PING",
        {"device_id": 9, "ip": "127.0.0.1", "device_type": "shelly_plug"},
        site=SITE + 1,
    )
    late = ordre(
        cloud_keys,
        "PING",
        {"device_id": 5, "ip": "127.0.0.1", "device_type": "shelly_plug"},
        expires=T0 - dt.timedelta(seconds=1),
    )
    canal.pending = [ping, wifi, forged, foreign, late]
    assert dc.poll() == 2
    assert alerts.count("DECISION_SIGNATURE_INVALID") == 2, "commande altérée ou d'un autre site : écartée et signalée"
    assert dc.poll() == 0, "une commande n'est exécutée qu'une fois, même servie à nouveau"
    by_id = {cid: body for cid, body in canal.results}
    assert by_id[json.loads(ping["payload"])["command_id"]]["status"] == "SUCCEEDED"
    assert by_id["5b1d2c3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"]["result"] == {
        "applied": True,
        "gen": 2,
        "restart_required": True,
    }
    assert "motdepasse-8" not in json.dumps(canal.results), "le mot de passe ne revient jamais au cloud"


def test_agent_annonce_sa_cle_de_commande_et_rend_le_resultat_par_le_sondage_signe(
    db, fake_cloud, publisher, clock, tmp_path, cloud_keys
):
    from .test_agent_db import TestOrchestration

    a = TestOrchestration().agent(db, fake_cloud, publisher, clock, tmp_path, cloud_keys)
    assert fake_cloud.last_command_key == a.command_key.public_key(), "clé publique X25519 remise au battement de cœur"
    fake_cloud.device_commands = [
        ordre(
            cloud_keys,
            "PING",
            {"device_id": 5, "ip": "192.0.2.10", "device_type": "shelly_plug"},
        )
    ]
    assert a.poll_commands() == 1
    a.device_commands.drain()
    ((_command_id, body),) = fake_cloud.command_results
    assert body["status"] == "FAILED" and "hors du réseau privé" in body["error"], (
        "adresse de documentation publique : jamais contactée"
    )


def test_commande_hors_reseau_prive_rendue_en_echec(identity, cloud_keys, tmp_path):
    canal = Canal()
    dc = commands.DeviceCommands(
        canal,
        lambda: identity,
        commands.CommandKey.load_or_create(tmp_path / "c.pem"),
        commands.Executor(timeout_s=0.2),
        FixedClock(T0).now,
        None,
        inline=True,
    )
    canal.pending = [ordre(cloud_keys, "SCAN", None, {"subnet": "8.8.8.0/24"})]
    assert dc.poll() == 1
    body = canal.results[0][1]
    assert body["status"] == "FAILED" and "privé" in body["error"]


def test_le_releve_des_commandes_laisse_une_trace_datee(identity, cloud_keys, tmp_path):
    """Un battement de cœur dit que le processus vit ; ``last_poll_at`` dit qu'il relève ses commandes.

    C'est la différence qui manquait le 02/10/2026 : agent « en ligne », commandes PENDING → EXPIRED. L'horodatage
    n'avance qu'à la FIN d'un relevé : un canal qui lève pendant ``fetch`` le laisse où il était."""
    canal = Canal()
    clock = FixedClock(T0)
    dc = commands.DeviceCommands(
        canal,
        lambda: identity,
        commands.CommandKey.load_or_create(tmp_path / "c.pem"),
        commands.Executor(timeout_s=0.2),
        clock.now,
        None,
        inline=True,
    )
    assert dc.last_poll_at is None and dc.executed == 0
    assert dc.poll() == 0
    assert dc.last_poll_at == T0, "un relevé vide est quand même un relevé réussi"

    class CanalMort(Canal):
        def fetch(self) -> list[dict]:
            raise ConnectionError("courtier parti")

    clock.set(T0 + dt.timedelta(minutes=1))
    dc._channel = CanalMort()
    with pytest.raises(ConnectionError):
        dc.poll()
    assert dc.last_poll_at == T0, "un relevé qui lève ne doit PAS dater le dernier succès"


def test_commands_stale_ne_juge_quapres_le_delai_de_grace():
    """Jamais relevé compris, mais pas avant d'avoir eu sa chance ; au-delà de deux durées de vie, c'est un canal mort."""
    seuil = commands.COMMANDS_STALE_AFTER_S
    # Démarrage récent : on ne sait pas encore.
    assert commands.commands_stale(None, T0, since_start_s=seuil) is False
    # Passé le délai de grâce, jamais relevé = périmé.
    assert commands.commands_stale(None, T0, since_start_s=seuil + 1) is True
    # Un relevé récent rassure…
    assert commands.commands_stale(T0 - dt.timedelta(seconds=seuil), T0, since_start_s=10_000) is False
    # … un relevé trop ancien non.
    assert commands.commands_stale(T0 - dt.timedelta(seconds=seuil + 1), T0, since_start_s=10_000) is True
