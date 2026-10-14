"""Découverte mDNS / DNS-SD des équipements du réseau local (RFC 6762, RFC 6763 ; E011, E019) : prises Shelly (``_shelly._tcp``)
et services HTTP (``_http._tcp``) annoncés sur le lien.

Implémentation minimale sans dépendance (la bibliothèque ``zeroconf`` n'est pas embarquée sur le boîtier) :

* **Requête** : une question PTR par service, bit « réponse unicast » (QU, RFC 6762 §5.4) posé, envoyée au groupe
  ``224.0.0.251:5353`` avec un TTL IP de 255 (lien local). Socket liée au port 5353 partagé (``SO_REUSEADDR`` / ``SO_REUSEPORT``,
  cohabite avec avahi) et abonnée au groupe : les réponses multicast comme unicast sont reçues. Si le port 5353 est indisponible,
  socket éphémère : seules les réponses « legacy unicast » (§6.7) arrivent, et le résultat le dit (``mode``).
* **Réponses** : enregistrements PTR, SRV, TXT et A des sections réponse et additionnelle, noms compressés suivis (sauts bornés) ;
  une instance est retenue avec l'adresse IPv4 de sa cible SRV, sinon celle de l'émetteur de la réponse.
* **Bornes** : durée totale bornée (``MAX_DURATION_S``), 512 datagrammes, 254 équipements, datagrammes de 9 000 octets au plus ;
  adresses hors du réseau privé du site écartées (politique ``allow`` de l'exécuteur).

Le transport est un port (``send`` / ``recv`` / ``close``) : les bancs de test rejouent des réponses mDNS simulées.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

LOG = logging.getLogger("h2s_agent.mdns")

GROUP = "224.0.0.251"
PORT = 5353
SERVICES = ("_shelly._tcp.local", "_http._tcp.local")
MAX_DURATION_S = 5.0
MAX_PACKETS = 512
MAX_SERVICES = 254
MAX_DATAGRAM = 9000
MAX_POINTER_JUMPS = 32

T_A, T_PTR, T_TXT, T_SRV = 1, 12, 16, 33
CLASS_IN = 1
QU_BIT = 0x8000
CACHE_FLUSH = 0x8000


class Transport(Protocol):
    """Échange de datagrammes mDNS : ``send`` au groupe, ``recv`` d'un datagramme (ou ``None`` à l'échéance), ``close``."""

    mode: str

    def send(self, data: bytes) -> None: ...

    def recv(self, timeout_s: float) -> tuple[bytes, str] | None: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------------------------------- codage


def encode_name(name: str) -> bytes:
    out = bytearray()
    for label in name.rstrip(".").split("."):
        raw = label.encode("utf-8")
        if not 0 < len(raw) < 64:
            raise ValueError(f"étiquette DNS invalide : {label!r}")
        out += bytes([len(raw)]) + raw
    return bytes(out + b"\x00")


def build_query(services: tuple[str, ...] = SERVICES, unicast_response: bool = True) -> bytes:
    """Requête DNS-SD : une question PTR par service (identifiant 0, RFC 6762 §18.1)."""
    header = struct.pack("!HHHHHH", 0, 0, len(services), 0, 0, 0)
    questions = b"".join(
        encode_name(s) + struct.pack("!HH", T_PTR, CLASS_IN | (QU_BIT if unicast_response else 0)) for s in services
    )
    return header + questions


# ---------------------------------------------------------------------------------------------------- lecture


@dataclass
class Record:
    name: str
    rtype: int
    data: object


def _read_name(msg: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    jumps = 0
    end: int | None = None
    pos = offset
    while True:
        if pos >= len(msg):
            raise ValueError("nom DNS tronqué")
        length = msg[pos]
        if length & 0xC0 == 0xC0:
            if pos + 1 >= len(msg):
                raise ValueError("pointeur DNS tronqué")
            jumps += 1
            if jumps > MAX_POINTER_JUMPS:
                raise ValueError("boucle de compression DNS")
            if end is None:
                end = pos + 2
            pos = ((length & 0x3F) << 8) | msg[pos + 1]
            continue
        if length & 0xC0:
            raise ValueError("étiquette DNS de type inconnu")
        if length == 0:
            return ".".join(labels), (end if end is not None else pos + 1)
        if pos + 1 + length > len(msg):
            raise ValueError("étiquette DNS tronquée")
        labels.append(msg[pos + 1 : pos + 1 + length].decode("utf-8", errors="replace"))
        if len(labels) > 127:
            raise ValueError("nom DNS trop long")
        pos += 1 + length


def parse_message(msg: bytes) -> list[Record]:
    """Enregistrements PTR, SRV, TXT et A des sections réponse, autorité et additionnelle ; ``ValueError`` si le message est illisible."""
    if len(msg) < 12:
        raise ValueError("message DNS trop court")
    _, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", msg[:12])
    if not flags & 0x8000:
        return []  # une requête d'un autre hôte, pas une réponse
    pos = 12
    for _ in range(qd):
        _, pos = _read_name(msg, pos)
        pos += 4
    records: list[Record] = []
    for _ in range(an + ns + ar):
        name, pos = _read_name(msg, pos)
        if pos + 10 > len(msg):
            raise ValueError("enregistrement DNS tronqué")
        rtype, _rclass, _ttl, rdlen = struct.unpack("!HHIH", msg[pos : pos + 10])
        pos += 10
        rdata_end = pos + rdlen
        if rdata_end > len(msg):
            raise ValueError("données DNS tronquées")
        if rtype == T_PTR:
            records.append(Record(name.lower(), rtype, _read_name(msg, pos)[0]))
        elif rtype == T_SRV and rdlen >= 7:
            _prio, _weight, port = struct.unpack("!HHH", msg[pos : pos + 6])
            records.append(Record(name.lower(), rtype, (port, _read_name(msg, pos + 6)[0])))
        elif rtype == T_TXT:
            txt: dict[str, str] = {}
            p = pos
            while p < rdata_end:
                n = msg[p]
                entry = msg[p + 1 : min(p + 1 + n, rdata_end)].decode("utf-8", errors="replace")
                key, _, value = entry.partition("=")
                if key:
                    txt[key.lower()[:40]] = value[:100]
                p += 1 + n
            records.append(Record(name.lower(), rtype, txt))
        elif rtype == T_A and rdlen == 4:
            records.append(Record(name.lower(), rtype, str(ipaddress.IPv4Address(msg[pos : pos + 4]))))
        pos = rdata_end
    return records


@dataclass
class Service:
    """Instance annoncée : nom d'instance, service, hôte et port (SRV), adresse IPv4, TXT."""

    instance: str
    service: str
    ip: str | None = None
    host: str | None = None
    port: int | None = None
    txt: dict[str, str] = field(default_factory=dict)


def resolve(packets: list[tuple[bytes, str]], services: tuple[str, ...] = SERVICES) -> list[Service]:
    """Instances des services demandés, reconstituées sur l'ensemble des datagrammes reçus (réponses et additionnels)."""
    wanted = {s.lower() for s in services}
    found: dict[str, Service] = {}
    srv: dict[str, tuple[int, str]] = {}
    txt: dict[str, dict[str, str]] = {}
    addresses: dict[str, str] = {}
    origin: dict[str, str] = {}
    for data, source in packets:
        try:
            records = parse_message(data)
        except (ValueError, struct.error, IndexError) as exc:
            LOG.debug("réponse mDNS illisible de %s : %s", source, exc)
            continue
        for r in records:
            if r.rtype == T_PTR and r.name in wanted and len(found) < MAX_SERVICES:
                instance = str(r.data).lower()
                found.setdefault(instance, Service(instance=str(r.data), service=r.name))
                origin.setdefault(instance, source)
            elif r.rtype == T_SRV:
                srv[r.name] = r.data  # type: ignore[assignment]
                origin.setdefault(r.name, source)
            elif r.rtype == T_TXT:
                txt[r.name] = r.data  # type: ignore[assignment]
            elif r.rtype == T_A:
                addresses.setdefault(r.name, str(r.data))
    out: list[Service] = []
    for key, s in found.items():
        if key in srv:
            s.port, s.host = srv[key][0], srv[key][1]
        s.txt = txt.get(key, {})
        s.ip = addresses.get((s.host or "").lower()) or origin.get(key)
        out.append(s)
    return out


# ---------------------------------------------------------------------------------------------------- transport réseau


class MulticastTransport:
    """Socket UDP du lien local : port 5353 partagé et groupe 224.0.0.251 si possible, sinon port éphémère (legacy unicast)."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        try:
            self._sock.bind(("", PORT))
            membership = struct.pack("4s4s", socket.inet_aton(GROUP), socket.inet_aton("0.0.0.0"))
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            self.mode = "multicast"
        except OSError:
            self._sock.close()
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            self._sock.bind(("", 0))
            self.mode = "unicast"
        self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)

    def send(self, data: bytes) -> None:
        self._sock.sendto(data, (GROUP, PORT))

    def recv(self, timeout_s: float) -> tuple[bytes, str] | None:
        self._sock.settimeout(max(0.01, timeout_s))
        try:
            data, addr = self._sock.recvfrom(MAX_DATAGRAM)
            return data, addr[0]
        except TimeoutError:
            return None

    def close(self) -> None:
        self._sock.close()


# ---------------------------------------------------------------------------------------------------- découverte


def discover(
    timeout_s: float = 1.5,
    services: tuple[str, ...] = SERVICES,
    transport_factory: Callable[[], Transport] = MulticastTransport,
    allow: Callable[[ipaddress.IPv4Address], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Requête DNS-SD puis écoute bornée ; rend ``{"mode", "responders", "services": [Service], "error"}``, jamais d'exception réseau."""
    duration = max(0.0, min(float(timeout_s), MAX_DURATION_S))
    try:
        transport = transport_factory()
    except OSError as exc:
        return {
            "mode": None,
            "responders": 0,
            "services": [],
            "error": f"mDNS indisponible : {exc}"[:200],
        }
    packets: list[tuple[bytes, str]] = []
    error = None
    try:
        query = build_query(services)
        transport.send(query)
        deadline = clock() + duration
        resent = False
        while len(packets) < MAX_PACKETS:
            left = deadline - clock()
            if left <= 0:
                break
            if not resent and left < duration / 2:
                transport.send(query)  # RFC 6762 §5.2 : une seconde question rattrape un datagramme perdu
                resent = True
            got = transport.recv(min(left, 0.25))
            if got is not None and len(got[0]) <= MAX_DATAGRAM:
                packets.append(got)
    except OSError as exc:
        error = f"mDNS interrompu : {exc}"[:200]
    finally:
        transport.close()
    kept: list[Service] = []
    for s in resolve(packets, services):
        try:
            ip = ipaddress.ip_address(s.ip) if s.ip else None
        except ValueError:
            ip = None
        if ip is None or ip.version != 4 or (allow is not None and not allow(ip)):
            continue
        s.ip = str(ip)
        kept.append(s)
    return {
        "mode": getattr(transport, "mode", None),
        "responders": len({src for _, src in packets}),
        "services": kept,
        "error": error,
    }
