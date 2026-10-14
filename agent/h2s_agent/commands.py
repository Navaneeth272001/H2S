"""Commandes aux équipements du site (spéc. 2 §4.3.2 et §4.3.3 ; E010, E011, E018, E019, E020) : test de connexion (PING),
recherche sur le réseau local (SCAN) et configuration Wi-Fi d'une prise Shelly (WIFI_CONFIG), exécutés par l'agent, seul à voir
le réseau privé du site.

* **Ordre** : charge utile signée par la clé des ordres du cloud (Ed25519, comme une décision), vérifiée ici ; destinée au site de
  l'agent, non échue, exécutée une seule fois. Un ordre faux est écarté et signalé, jamais exécuté.
* **Transport** : port ``CommandChannel`` (``fetch`` / ``report``). Aujourd'hui le sondage signé HTTP de l'API agent
  (``HttpCommandChannel``) ; au lot 5, un transport MQTT porte les mêmes charges utiles signées sans changer ce module.
* **Réseau** : jamais hors du réseau privé du site (RFC 1918, IPv6 ULA ou lien local) ; recherche bornée à un /24, débit borné
  (16 connexions à la fois, délai court) : sondage HTTP ``/shelly`` et Modbus TCP 502, fusionné avec la découverte mDNS / DNS-SD
  (``_shelly._tcp``, ``_http._tcp`` ; module ``mdns``, écoute bornée en durée) menée en parallèle.
* **Secret** : le mot de passe Wi-Fi arrive chiffré pour la clé de commande de l'agent (X25519, HKDF-SHA256, AES-256-GCM lié à
  l'identifiant de la commande), n'est déchiffré qu'au moment de l'appel à la prise, jamais journalisé ni rendu au cloud.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import ipaddress
import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import mdns as mdns_discovery
from .clock import parse_instant
from .crypto import b64url, b64url_decode, verify_signed_payload

LOG = logging.getLogger("h2s_agent.commands")

TYPES = ("PING", "SCAN", "WIFI_CONFIG")
SECRET_ALG = "X25519-HKDF-SHA256+A256GCM"
SECRET_INFO = b"H2S-COMMAND-SECRET-V1"
SCAN_WORKERS = 16
MAX_HOSTS = 254
MDNS_TIMEOUT_S = 1.5


class CommandRefused(ValueError):
    """Commande refusée avant toute action (cible hors du réseau privé, paramètres invalides) : rendue en échec."""


# ---------------------------------------------------------------------------------------------------- réseau privé du site


def private_lan(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Adresse d'un équipement du réseau local : privée (RFC 1918, ULA) ou lien local ; ni boucle locale, ni publique."""
    if ip.is_loopback or ip.is_multicast or ip.is_unspecified:
        return False
    if ip.version == 4:
        return any(
            ip in n
            for n in (
                ipaddress.ip_network("10.0.0.0/8"),
                ipaddress.ip_network("172.16.0.0/12"),
                ipaddress.ip_network("192.168.0.0/16"),
                ipaddress.ip_network("169.254.0.0/16"),
            )
        )
    return ip in ipaddress.ip_network("fc00::/7") or ip.is_link_local


# ---------------------------------------------------------------------------------------------------- clé et secret


class CommandKey:
    """Clé X25519 de l'agent pour les secrets de commande, dans un fichier 0600 (sa partie publique part au cloud au battement de cœur)."""

    def __init__(self, key: x25519.X25519PrivateKey) -> None:
        self._key = key

    @classmethod
    def from_private_bytes(cls, raw: bytes) -> CommandKey:
        return cls(x25519.X25519PrivateKey.from_private_bytes(raw))

    @classmethod
    def load_or_create(cls, path: Path) -> CommandKey:
        path = Path(path)
        if path.exists():
            if path.stat().st_mode & 0o077:
                raise PermissionError(f"clé de commande lisible par d'autres comptes : chmod 600 {path}")
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(key, x25519.X25519PrivateKey):
                raise ValueError("clé de commande : X25519 attendue")
            return cls(key)
        key = x25519.X25519PrivateKey.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
        return cls(key)

    def public_key(self) -> str:
        return b64url(self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))

    def exchange(self, peer_raw: bytes) -> bytes:
        return self._key.exchange(x25519.X25519PublicKey.from_public_bytes(peer_raw))


def _derive(shared: bytes, epk: bytes, agent_pub: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=epk + agent_pub, info=SECRET_INFO).derive(shared)


def decrypt_secret(key: CommandKey, envelope: dict, command_id: str) -> str:
    """Secret d'une commande ; ``ValueError`` si l'enveloppe est fausse, d'une autre commande ou d'une autre clé."""
    try:
        if envelope.get("alg") != SECRET_ALG:
            raise ValueError("algorithme de secret inconnu")
        epk = b64url_decode(envelope["epk"])
        k = _derive(key.exchange(epk), epk, b64url_decode(key.public_key()))
        return (
            AESGCM(k)
            .decrypt(
                b64url_decode(envelope["iv"]),
                b64url_decode(envelope["ct"]),
                command_id.encode(),
            )
            .decode("utf-8")
        )
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("secret de commande indéchiffrable") from exc
    except Exception as exc:
        raise ValueError("secret de commande indéchiffrable") from exc


def encrypt_secret(agent_public_b64url: str, clear: str, command_id: str) -> dict:
    """Chiffrement pour la clé de commande d'un agent (miroir de ``DeviceCommandCrypto`` du cloud ; bancs de test)."""
    agent_pub = b64url_decode(agent_public_b64url)
    eph = x25519.X25519PrivateKey.generate()
    epk = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    k = _derive(
        eph.exchange(x25519.X25519PublicKey.from_public_bytes(agent_pub)),
        epk,
        agent_pub,
    )
    iv = os.urandom(12)
    return {
        "alg": SECRET_ALG,
        "epk": b64url(epk),
        "iv": b64url(iv),
        "ct": b64url(AESGCM(k).encrypt(iv, clear.encode(), command_id.encode())),
    }


# ---------------------------------------------------------------------------------------------------- exécution


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse toute redirection HTTP d'un appareil : la réponse 3xx devient une erreur (sonde sans résultat)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirection refusée", headers, fp)


class Executor:
    """Actions réseau sur les équipements. ``allow`` : politique d'adresse (réseau privé du site ; bancs de test : boucle locale)."""

    def __init__(
        self,
        http_port: int = 80,
        modbus_port: int = 502,
        timeout_s: float = 2.0,
        allow: Callable[[ipaddress.IPv4Address | ipaddress.IPv6Address], bool] = private_lan,
        mdns: Callable[[float], dict] | None = None,
        mdns_timeout_s: float = MDNS_TIMEOUT_S,
    ) -> None:
        self._http = http_port
        self._modbus = modbus_port
        self._timeout = timeout_s
        self._allow = allow
        # Découverte mDNS : écoute du lien local bornée en durée ; remplaçable (bancs de test : annonces simulées).
        self._mdns = mdns if mdns is not None else (lambda t: mdns_discovery.discover(t, allow=allow))
        self._mdns_timeout = mdns_timeout_s

    def _address(self, raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
        try:
            ip = ipaddress.ip_address(str(raw).strip())
        except ValueError as exc:
            raise CommandRefused("adresse IP invalide") from exc
        if not self._allow(ip):
            raise CommandRefused(f"adresse {ip} hors du réseau privé du site : refusée")
        return ip

    def _connect(self, ip: str, port: int, timeout: float) -> float | None:
        started = time.monotonic()
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return (time.monotonic() - started) * 1000
        except OSError:
            return None

    def _json(self, url: str, timeout: float, body: bytes | None = None) -> dict | None:
        request = urllib.request.Request(
            url,
            data=body,
            method="POST" if body is not None else "GET",
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        # Pas de mandataire : une prise du réseau local ne passe jamais par un proxy HTTP du boîtier. Pas de redirection
        # suivie (S9) : un hôte du LAN ne fait pas interroger au boîtier une autre adresse (boucle locale, API de l'agent).
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=timeout) as r:
                data = json.loads(r.read(65_536).decode("utf-8"))
                return data if isinstance(data, dict) else None
        except (urllib.error.URLError, OSError, ValueError, UnicodeDecodeError):
            return None

    def _host(self, ip: str) -> str:
        return f"[{ip}]" if ":" in ip else ip

    def _shelly(self, ip: str, timeout: float) -> dict | None:
        info = self._json(f"http://{self._host(ip)}:{self._http}/shelly", timeout)
        if info is None or not ("mac" in info and ("gen" in info or "type" in info)):
            return None
        gen = int(info.get("gen") or 1)
        mac = str(info.get("mac", "")).upper()
        if len(mac) == 12:
            mac = ":".join(mac[i : i + 2] for i in range(0, 12, 2))
        return {
            "gen": gen,
            "mac": mac[:17],
            "model": str(info.get("model") or info.get("type") or "")[:100] or None,
            "name": str(info.get("id") or info.get("name") or "")[:100] or None,
            "firmware": str(info.get("fw_id") or info.get("fw") or "")[:50] or None,
        }

    def ping(self, raw_ip: str, device_type: str | None) -> dict:
        """Test de connexion : port HTTP (prise Shelly, fiche ``/shelly`` lue) ou Modbus TCP 502 (centrale PowerLogic)."""
        ip = str(self._address(raw_ip))
        kind = (device_type or "").lower()
        ports = (
            [self._modbus] if "powerlogic" in kind or "modbus" in kind or kind == "iem" else [self._http, self._modbus]
        )
        for port in ports:
            latency = self._connect(ip, port, self._timeout)
            if latency is None:
                continue
            out = {"reachable": True, "port": port, "latency_ms": round(latency, 1)}
            if port == self._http:
                shelly = self._shelly(ip, self._timeout)
                if shelly is not None:
                    out["device"] = shelly
            return out
        return {"reachable": False, "port": None, "latency_ms": None}

    def scan(self, subnet: str) -> dict:
        """Recherche des équipements d'un réseau privé /24 au plus : prises Shelly (HTTP ``/shelly``), Modbus TCP (502) et annonces mDNS.

        La découverte mDNS tourne en parallèle du sondage ; ses annonces du réseau recherché sont fusionnées par adresse
        (``discovery`` dit qui a trouvé l'équipement). Une prise annoncée dont la fiche ``/shelly`` ne répond pas est décrite par
        son enregistrement TXT. mDNS indisponible : la recherche reste un sondage, et ``mdns.error`` le dit.
        """
        try:
            network = ipaddress.ip_network(str(subnet), strict=False)
        except ValueError as exc:
            raise CommandRefused("réseau à rechercher invalide") from exc
        hosts = list(network.hosts())
        if network.version != 4 or network.num_addresses > 256 or not hosts:
            raise CommandRefused("recherche limitée à un réseau IPv4 privé de 254 adresses au plus (/24)")
        if not all(self._allow(h) for h in (hosts[0], hosts[-1])):
            raise CommandRefused(f"réseau {network} hors du réseau privé du site : refusé")
        probe_timeout = min(self._timeout, 0.5)

        def probe(ip: ipaddress.IPv4Address) -> dict | None:
            host = str(ip)
            if self._connect(host, self._http, probe_timeout) is not None:
                shelly = self._shelly(host, max(probe_timeout, 1.0))
                if shelly is not None:
                    return {
                        "ip": host,
                        "type": "shelly_plug",
                        "name": shelly["name"],
                        "mac": shelly["mac"],
                        "model": shelly["model"],
                        "gen": shelly["gen"],
                        "firmware": shelly["firmware"],
                        "online": True,
                        "discovery": ["http"],
                    }
            if self._connect(host, self._modbus, probe_timeout) is not None:
                return {
                    "ip": host,
                    "type": "modbus_tcp",
                    "name": None,
                    "mac": None,
                    "model": None,
                    "gen": None,
                    "firmware": None,
                    "online": True,
                    "discovery": ["modbus"],
                }
            return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=SCAN_WORKERS + 1) as pool:
            announced = pool.submit(self._announced, network)
            found = [h for h in pool.map(probe, hosts[:MAX_HOSTS]) if h is not None]
            services, summary = announced.result()
        by_ip = {h["ip"]: h for h in found}
        for s in services:
            if len(by_ip) >= MAX_HOSTS:
                break
            known = by_ip.get(s.ip)
            if known is not None:
                if "mdns" not in known["discovery"]:
                    known["discovery"].append("mdns")
                known["name"] = known["name"] or _instance_label(s)
                continue
            by_ip[s.ip] = self._from_announcement(s)
        hosts_out = sorted(by_ip.values(), key=lambda h: ipaddress.ip_address(h["ip"]))
        return {
            "subnet": str(network),
            "scanned": min(len(hosts), MAX_HOSTS),
            "hosts": hosts_out,
            "mdns": summary,
        }

    def _announced(self, network: ipaddress.IPv4Network) -> tuple[list, dict]:
        """Annonces mDNS du réseau recherché, une par adresse ; jamais d'exception (le sondage continue sans elles)."""
        try:
            out = self._mdns(self._mdns_timeout) or {}
        except Exception as exc:  # noqa: BLE001 - interface sans multicast, pare-feu : la recherche reste un sondage
            out = {
                "mode": None,
                "responders": 0,
                "services": [],
                "error": f"mDNS indisponible : {exc}"[:200],
            }
        services = list(out.get("services") or [])
        kept: list = []
        seen: set[str] = set()
        for s in services:
            try:
                ip = ipaddress.ip_address(str(s.ip))
            except ValueError:
                continue
            if ip.version != 4 or ip not in network or not self._allow(ip) or str(ip) in seen:
                continue
            seen.add(str(ip))
            s.ip = str(ip)
            kept.append(s)
        summary = {
            "mode": out.get("mode"),
            "responders": int(out.get("responders") or 0),
            "announced": len(services),
            "error": out.get("error"),
        }
        return kept, summary

    @staticmethod
    def _from_announcement(s) -> dict:
        """Équipement trouvé par son annonce seule (le sondage HTTP de la même adresse n'a pas lu de fiche ``/shelly``) : décrit par
        l'enregistrement TXT de l'annonce, sans nouvelle connexion (la recherche reste bornée en durée)."""
        is_shelly = s.service.startswith("_shelly.") or _instance_label(s).lower().startswith("shelly")
        txt = s.txt or {}
        gen = txt.get("gen")
        return {
            "ip": s.ip,
            "type": "shelly_plug" if is_shelly else "http_device",
            "name": _instance_label(s)[:100] or None,
            "mac": None,
            "model": (txt.get("app") or txt.get("model") or "")[:100] or None,
            "gen": int(gen) if gen and gen.isdigit() and len(gen) == 1 else None,
            "firmware": (txt.get("ver") or txt.get("fw") or "")[:50] or None,
            "online": True,
            "discovery": ["mdns"],
        }

    def wifi(self, raw_ip: str, ssid: str, password: str) -> dict:
        """Réseau Wi-Fi de la prise : RPC Gen2 ``WiFi.SetConfig`` ; Gen1 ``/settings/sta``. Le mot de passe n'est jamais journalisé."""
        ip = str(self._address(raw_ip))
        if not 1 <= len(ssid.encode("utf-8")) <= 32 or not 8 <= len(password) <= 63:
            raise CommandRefused("SSID de 1 à 32 octets et mot de passe de 8 à 63 caractères attendus")
        shelly = self._shelly(ip, self._timeout)
        if shelly is None:
            raise CommandRefused("aucune prise Shelly ne répond à cette adresse")
        base = f"http://{self._host(ip)}:{self._http}"
        if shelly["gen"] >= 2:
            body = json.dumps({"config": {"sta": {"ssid": ssid, "pass": password, "enable": True}}}).encode()
            out = self._json(base + "/rpc/WiFi.SetConfig", self._timeout, body)
            if out is None:
                raise CommandRefused("la prise a refusé la configuration Wi-Fi (RPC WiFi.SetConfig)")
            return {
                "applied": True,
                "gen": shelly["gen"],
                "restart_required": bool(out.get("restart_required", False)),
            }
        # Limite de l'API Shelly Gen1 (S9) : mot de passe dans l'URL d'une requête HTTP du LAN, visible sur le réseau local
        # pendant l'envoi ; préférer Gen2 ou un réseau d'appairage isolé (edge/deploy/README.md §5).
        query = urllib.parse.urlencode({"enabled": 1, "ssid": ssid, "key": password})
        out = self._json(base + "/settings/sta?" + query, self._timeout)
        if out is None:
            raise CommandRefused("la prise a refusé la configuration Wi-Fi (/settings/sta)")
        return {"applied": True, "gen": shelly["gen"], "restart_required": False}


# ---------------------------------------------------------------------------------------------------- canal et orchestration


class CommandChannel(Protocol):
    """Transport des commandes : ``fetch`` rend les commandes signées en attente, ``report`` rend le résultat d'une commande."""

    def fetch(self) -> list[dict]: ...

    def report(self, command_id: str, body: dict) -> None: ...


class HttpCommandChannel:
    """Sondage signé de l'API agent (``GET /api/agent/commands``, ``POST /api/agent/commands/{id}/result``)."""

    def __init__(self, cloud) -> None:
        self._cloud = cloud

    def fetch(self) -> list[dict]:
        return list((self._cloud.commands() or {}).get("commands") or [])

    def report(self, command_id: str, body: dict) -> None:
        self._cloud.command_result(command_id, body)


#: Durée de vie d'une commande côté cloud (DeviceCommandService.TTL) ; un test lit la source Java et vérifie l'égalité.
COMMAND_TTL_S = 120
#: Au-delà de deux durées de vie sans relevé réussi, l'agent ne traite plus ses commandes — quoi qu'en dise son
#: battement de cœur. Deux, parce qu'un seul relevé manqué est un aléa ; deux de suite, c'est un canal mort.
COMMANDS_STALE_AFTER_S = 2 * COMMAND_TTL_S


def commands_stale(last_poll_at: dt.datetime | None, now: dt.datetime, since_start_s: float) -> bool:
    """Le relevé des commandes a-t-il cessé ? Pure, pour être testée sans agent.

    Vrai quand aucun relevé n'a RÉUSSI depuis ``COMMANDS_STALE_AFTER_S`` — jamais relevé compris, une fois passé le
    même délai depuis le démarrage (un agent qui vient de partir n'a pas encore eu sa chance). C'est la situation du
    02/10/2026 : agent « en ligne », battement de cœur à la minute, et des commandes qui mouraient PENDING →
    EXPIRED sans qu'aucun indicateur ne bouge. Un battement de cœur dit que le processus vit, pas qu'il fait son
    travail ; cet indicateur-ci dit la seconde chose.
    """
    if since_start_s <= COMMANDS_STALE_AFTER_S:
        return False
    if last_poll_at is None:
        return True
    return (now - last_poll_at).total_seconds() > COMMANDS_STALE_AFTER_S


class DeviceCommands:
    """Vérifie, exécute (sur un fil à part : une recherche ne retarde pas le chien de garde des batteries) et rend les commandes."""

    SEEN_MAX = 1000

    def __init__(
        self,
        channel: CommandChannel,
        identity: Callable[[], object | None],
        key: CommandKey,
        executor: Executor,
        now: Callable[[], dt.datetime],
        alert: Callable[..., object] | None = None,
        inline: bool = False,
    ) -> None:
        self._channel = channel
        self._identity = identity
        self._key = key
        self._executor = executor
        self._now = now
        self._alert = alert or (lambda code, severity, message, **kw: None)
        self._inline = inline
        self._seen: dict[str, dt.datetime] = {}
        self._lock = threading.Lock()
        self._pool = (
            None if inline else concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="h2s-commands")
        )
        self._futures: list[concurrent.futures.Future] = []
        #: Dernier relevé qui a ABOUTI (le canal a répondu, les commandes ont été lues) ; None tant qu'aucun n'a réussi.
        self.last_poll_at: dt.datetime | None = None
        #: Commandes vérifiées et lancées depuis le démarrage.
        self.executed = 0

    def poll(self) -> int:
        """Commandes nouvelles vérifiées et lancées ; renvoie leur nombre.

        ``last_poll_at`` n'avance qu'à la fin : un canal qui lève pendant ``fetch`` laisse l'horodatage où il était,
        et c'est ce qui permet à :func:`commands_stale` de voir un relevé qui n'aboutit plus.
        """
        ident = self._identity()
        if ident is None:
            return 0
        started = 0
        now = self._now()
        for item in self._channel.fetch():
            try:
                command = self._verify(ident, item, now)
            except ValueError as exc:
                self._alert(
                    "DECISION_SIGNATURE_INVALID",
                    "CRITICAL",
                    f"commande d'équipement écartée : {exc}",
                )
                continue
            if command is None:
                continue
            started += 1
            if self._inline:
                self._run(command)
            else:
                self._futures = [f for f in self._futures if not f.done()] + [self._pool.submit(self._run, command)]
        with self._lock:
            self.last_poll_at = now
            self.executed += started
        return started

    def drain(self, timeout_s: float = 30.0) -> None:
        """Attend la fin des commandes lancées (arrêt du service, bancs de test)."""
        concurrent.futures.wait(list(self._futures), timeout=timeout_s)

    def _verify(self, ident, item: dict, now: dt.datetime) -> dict | None:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("payload"), str)
            or not isinstance(item.get("signature"), str)
        ):
            raise ValueError("commande mal formée")  # noqa: TRY004 - contenu mal formé, pas un type d'argument
        if item.get("key_id") and item["key_id"] != ident.decision_key_id:
            raise ValueError("clé de signature inconnue")
        p = verify_signed_payload(ident.decision_public_key, item["payload"], item["signature"])
        if p.get("type") != "device_command" or p.get("v") != 1 or p.get("command") not in TYPES:
            raise ValueError("charge utile qui n'est pas une commande d'équipement")
        if int(p.get("site_id", -1)) != ident.site_id:
            raise ValueError("commande destinée à un autre site")
        command_id = str(uuid.UUID(str(p["command_id"])))
        with self._lock:
            if command_id in self._seen:
                return None
            if parse_instant(p["expires_at"]) <= now:
                return None  # échue : le cloud la marque expirée, rien n'est tenté
            self._seen[command_id] = now
            if len(self._seen) > self.SEEN_MAX:
                for old in sorted(self._seen, key=self._seen.get)[: len(self._seen) - self.SEEN_MAX]:
                    del self._seen[old]
        return dict(p, command_id=command_id)

    def _run(self, c: dict) -> None:
        started = time.monotonic()
        target = c.get("target") or {}
        params = c.get("params") or {}
        try:
            if c["command"] == "PING":
                result = self._executor.ping(str(target.get("ip")), target.get("device_type"))
            elif c["command"] == "SCAN":
                result = self._executor.scan(str(params.get("subnet") or local_subnet()))
            else:
                secret = c.get("secret")
                if not isinstance(secret, dict):
                    raise CommandRefused("mot de passe Wi-Fi absent de la commande")
                password = decrypt_secret(self._key, secret, c["command_id"])
                try:
                    result = self._executor.wifi(str(target.get("ip")), str(params.get("ssid", "")), password)
                finally:
                    del password
            body = {"status": "SUCCEEDED", "result": result, "error": None}
        except (CommandRefused, ValueError) as exc:
            body = {"status": "FAILED", "result": None, "error": str(exc)[:300]}
        except Exception:
            LOG.exception("commande %s en échec", c["command"])
            body = {
                "status": "FAILED",
                "result": None,
                "error": "erreur interne de l'agent",
            }
        body["duration_ms"] = int((time.monotonic() - started) * 1000)
        try:
            self._channel.report(c["command_id"], body)
        except Exception as exc:  # noqa: BLE001 - cloud injoignable : le cloud expirera la commande, l'opérateur relance
            LOG.warning("résultat de la commande %s non rendu : %s", c["command_id"], exc)

    def stop(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)


def _instance_label(s) -> str:
    """Nom lisible d'une instance DNS-SD : « shellyplugsg3-aabbcc._shelly._tcp.local » → « shellyplugsg3-aabbcc »."""
    instance = str(s.instance or "")
    suffix = "." + str(s.service or "")
    return instance[: -len(suffix)] if suffix != "." and instance.lower().endswith(suffix.lower()) else instance


def local_subnet(probe_host: str = "192.0.2.1") -> str:
    """Réseau /24 de l'interface par défaut du boîtier (sans envoi de paquet : connexion UDP non émise)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((probe_host, 9))
        ip = ipaddress.ip_address(s.getsockname()[0])
    if not private_lan(ip):
        raise CommandRefused(f"interface du boîtier ({ip}) hors réseau privé : recherche refusée")
    return str(ipaddress.ip_network(f"{ip}/24", strict=False))
