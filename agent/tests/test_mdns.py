"""Découverte mDNS / DNS-SD de l'agent (E011, E019 ; RFC 6762, RFC 6763) sur des réponses simulées : requête PTR des services Shelly
et HTTP, lecture des enregistrements (noms compressés), instance reconstituée (SRV, TXT, A), durée bornée, adresses hors du réseau
privé écartées, datagrammes illisibles ignorés."""

from __future__ import annotations

import ipaddress
import struct

import pytest

from h2s_agent import commands, mdns


def rr(name: bytes, rtype: int, rdata: bytes, cache_flush: bool = False) -> bytes:
    return (
        name
        + struct.pack(
            "!HHIH",
            rtype,
            mdns.CLASS_IN | (mdns.CACHE_FLUSH if cache_flush else 0),
            120,
            len(rdata),
        )
        + rdata
    )


def shelly_announcement(ip: str = "192.168.1.50", instance: str = "shellyplugsg3-aabbcc") -> bytes:
    """Réponse d'une prise Shelly Gen2 telle qu'elle l'émet : PTR en réponse, SRV, TXT et A en additionnels, noms compressés."""
    header = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 3)
    service = mdns.encode_name("_shelly._tcp.local")  # offset 12
    ptr_name_offset = 12
    ptr_rdata = bytes([len(instance)]) + instance.encode() + struct.pack("!H", 0xC000 | ptr_name_offset)
    answer = rr(service, mdns.T_PTR, ptr_rdata)
    instance_offset = 12 + len(service) + 10  # début des données du PTR = nom de l'instance
    host = bytes([len(instance)]) + instance.encode() + b"\x05local\x00"
    srv = rr(
        struct.pack("!H", 0xC000 | instance_offset),
        mdns.T_SRV,
        struct.pack("!HHH", 0, 0, 80) + host,
        cache_flush=True,
    )
    host_offset = 12 + len(answer) + 2 + 10 + 6
    txt_data = b"".join(bytes([len(e)]) + e for e in (b"gen=2", b"app=PlugSG3", b"ver=1.4.4"))
    txt = rr(
        struct.pack("!H", 0xC000 | instance_offset),
        mdns.T_TXT,
        txt_data,
        cache_flush=True,
    )
    a = rr(
        struct.pack("!H", 0xC000 | host_offset),
        mdns.T_A,
        ipaddress.IPv4Address(ip).packed,
        cache_flush=True,
    )
    return header + answer + srv + txt + a


class TransportSimule:
    """Transport mDNS rejouant des datagrammes sur une horloge simulée ; garde les requêtes émises."""

    def __init__(self, packets: list[tuple[bytes, str]], clock: list[float], step_s: float = 0.2) -> None:
        self.mode = "multicast"
        self.sent: list[bytes] = []
        self.closed = False
        self._packets = list(packets)
        self._clock = clock
        self._step = step_s

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, timeout_s: float):
        self._clock[0] += min(timeout_s, self._step)
        return self._packets.pop(0) if self._packets else None

    def close(self) -> None:
        self.closed = True


def test_requete_ptr_shelly_et_http_avec_reponse_unicast():
    q = mdns.build_query()
    _, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", q[:12])
    assert (flags, qd, an, ns, ar) == (0, 2, 0, 0, 0)
    assert mdns.encode_name("_shelly._tcp.local") + struct.pack("!HH", mdns.T_PTR, 0x8001) in q
    assert mdns.encode_name("_http._tcp.local") + struct.pack("!HH", mdns.T_PTR, 0x8001) in q


def test_annonce_shelly_reconstituee_depuis_ptr_srv_txt_a():
    services = mdns.resolve([(shelly_announcement(), "192.168.1.50")])
    assert len(services) == 1
    s = services[0]
    assert s.instance == "shellyplugsg3-aabbcc._shelly._tcp.local" and s.service == "_shelly._tcp.local"
    assert (s.host, s.port, s.ip) == ("shellyplugsg3-aabbcc.local", 80, "192.168.1.50")
    assert s.txt == {"gen": "2", "app": "PlugSG3", "ver": "1.4.4"}


def test_sans_enregistrement_a_adresse_de_l_emetteur():
    header = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0)
    service = mdns.encode_name("_http._tcp.local")
    packet = header + rr(service, mdns.T_PTR, mdns.encode_name("Onduleur._http._tcp.local"))
    services = mdns.resolve([(packet, "192.168.1.77")])
    assert [(s.instance, s.ip, s.port) for s in services] == [("Onduleur._http._tcp.local", "192.168.1.77", None)]


def test_datagrammes_illisibles_ou_requetes_ignores():
    loop = (
        struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0)
        + struct.pack("!H", 0xC00C)
        + struct.pack("!HHIH", mdns.T_PTR, 1, 1, 2)
        + b"\xc0\x0c"
    )
    with pytest.raises(ValueError):
        mdns.parse_message(loop)
    assert mdns.parse_message(mdns.build_query()) == [], "une question d'un autre hôte n'est pas une réponse"
    tronque = shelly_announcement()[:40]
    good = shelly_announcement("192.168.1.51", "shellyplus1pm-ddeeff")
    services = mdns.resolve([(loop, "192.168.1.9"), (tronque, "192.168.1.9"), (good, "192.168.1.51")])
    assert [s.ip for s in services] == ["192.168.1.51"]


def test_decouverte_bornee_en_duree_adresses_publiques_ecartees():
    clock = [100.0]
    packets = [
        (shelly_announcement("192.168.1.50"), "192.168.1.50"),
        (b"\x00" * 5, "192.168.1.60"),
        (shelly_announcement("8.8.8.8", "shelly-public"), "8.8.8.8"),
    ] + [(b"", "192.168.1.61")] * 1000
    transports: list[TransportSimule] = []

    def factory():
        t = TransportSimule(packets, clock, step_s=0.2)
        transports.append(t)
        return t

    out = mdns.discover(
        timeout_s=1.0,
        transport_factory=factory,
        allow=commands.private_lan,
        clock=lambda: clock[0],
    )
    t = transports[0]
    assert clock[0] - 100.0 <= 1.0 + 0.25, "écoute arrêtée à l'échéance malgré un flot de datagrammes"
    assert t.closed and len(t.sent) == 2, "question renvoyée une fois à mi-parcours (RFC 6762 §5.2)"
    assert [s.ip for s in out["services"]] == ["192.168.1.50"], "adresse publique écartée"
    assert out["mode"] == "multicast" and out["error"] is None
    assert (
        mdns.discover(
            timeout_s=60,
            transport_factory=lambda: TransportSimule([], clock),
            clock=lambda: clock[0],
        )["services"]
        == []
    )
    assert clock[0] - 100.0 <= 1.25 + mdns.MAX_DURATION_S + 0.25, "durée plafonnée"


def test_mdns_indisponible_rendu_sans_exception():
    def refuse():
        raise OSError("réseau absent")

    out = mdns.discover(transport_factory=refuse)
    assert out["services"] == [] and "réseau absent" in out["error"]
