"""Collecte directe des compteurs (E172, E188 Linky local) : TIC Linky sur port série, PowerLogic en Modbus TCP et RTU.

Vecteurs : groupes TIC réels publiés (mode historique : ADCO…MOTDETAT ; mode standard : VTIC, PREF, IRMS1), trames Modbus
de la spécification (Modbus Application Protocol V1.1b3, lecture de registres ; CRC-16 de trames RTU connues). Serveur Modbus
TCP et esclave RTU simulés ici, port série simulé par un pseudo-terminal : aucun matériel.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import socketserver
import struct
import threading
import time

import pytest

from h2s_agent import config as config_mod
from h2s_agent import modbus, parsers, tic

T0 = dt.datetime(2026, 9, 14, 8, 0, tzinfo=dt.timezone.utc)

# Neuf groupes publiés, sommes comprises (vecteurs indépendants du code) ; ADCO (adresse du compteur) reconstruit avec sa somme.
HISTORIQUE = [
    b"ADCO 031762166834 F",
    b"OPTARIF BASE 0",
    b"ISOUSC 30 9",
    b"BASE 002809718 .",
    b"PTEC TH.. $",
    b"IINST 002 Y",
    b"IMAX 090 H",
    b"PAPP 00390 -",
    b"HHPHC A ,",
    b"MOTDETAT 000000 B",
]


def frame(groups: list[bytes]) -> bytes:
    return b"\x02" + b"".join(b"\n" + g + b"\r" for g in groups) + b"\x03"


def standard_group(label: str, value: str, horodate: str | None = None) -> bytes:
    body = (
        label.encode() + b"\t" + ((horodate.encode() + b"\t") if horodate is not None else b"") + value.encode() + b"\t"
    )
    return body + bytes([tic.checksum(body)])


class TestTic:
    def test_mode_historique_trame_reelle(self):
        f = tic.decode_frame(frame(HISTORIQUE), "historique")
        assert f.rejected == 0 and len(f.groups) == 10, "sommes de contrôle des groupes réels (Enedis-NOI-CPT_02E)"
        values = {r.data_type: r.value for r in tic.readings(7, f, "historique", T0)}
        assert values == {
            "energy_total_wh": 2809718.0,
            "current_a": 2.0,
            "apparent_power_va": 390.0,
        }
        assert tic.meter_id(f) == "031762166834"

    def test_mode_standard_sommes_de_la_norme_et_groupes_horodates(self):
        real = [b"VTIC\t02\tJ", b"PREF\t09\tH", b"IRMS1\t002\t0"]
        f = tic.decode_frame(frame(real), "standard")
        assert f.rejected == 0, (
            "groupes réels du mode standard : séparateur final compris dans la somme (Enedis-NOI-CPT_54E)"
        )
        groups = real + [
            standard_group("ADSC", "041876097342"),
            standard_group("DATE", "", "E260914100000"),
            standard_group("EAST", "003045839"),
            standard_group("EAIT", "000012000"),
            standard_group("URMS1", "231"),
            standard_group("SINSTS", "00477"),
            standard_group("SMAXSN", "05210", "E260914083000"),
            b"SINSTI\t00000\tZ",
        ]
        f = tic.decode_frame(frame(groups), "standard")
        assert f.rejected == 1, "somme fausse : groupe écarté, jamais lu"
        assert f.groups["DATE"] == ("", "E260914100000") and f.groups["SMAXSN"] == (
            "05210",
            "E260914083000",
        )
        values = {r.data_type: r.value for r in tic.readings(7, f, "standard", T0, expected_meter="041876097342")}
        assert values == {
            "energy_total_wh": 3045839.0,
            "energy_injected_total_wh": 12000.0,
            "current_a": 2.0,
            "voltage_v": 231.0,
            "apparent_power_va": 477.0,
        }, "pas de puissance active inventée"
        with pytest.raises(parsers.ParseError):
            tic.readings(7, f, "standard", T0, expected_meter="999999999999")

    def test_port_serie_simule_trames_decoupees_bruit_et_interruption(self):
        master, slave = os.openpty()
        got: list[tuple[list, dt.datetime]] = []
        reader = tic.TicReader(
            7,
            os.ttyname(slave),
            "historique",
            lambda readings, at: got.append((readings, at)) or len(readings),
            lambda: T0,
            lambda: True,
        )
        reader.start()
        try:
            data = b"bruit" + frame(HISTORIQUE[:4]) + b"\x02\nIINST 002 Y\r\x04" + frame(HISTORIQUE)
            for i in range(0, len(data), 7):
                os.write(master, data[i : i + 7])
                time.sleep(0.005)
            deadline = time.monotonic() + 5
            while len(got) < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            reader.stop()
            os.close(master)
            os.close(slave)
        assert len(got) == 2, "deux trames complètes ; la trame interrompue par EOT est ignorée"
        assert {r.data_type for r in got[1][0]} == {
            "energy_total_wh",
            "current_a",
            "apparent_power_va",
        }

    def test_configuration(self, tmp_path):
        path = tmp_path / "agent.json"
        path.write_text(
            json.dumps(
                {
                    "cloud_url": "https://api.h2s.example",
                    "db_url": "postgresql://x@/y",
                    "devices": [
                        {
                            "device_id": 7,
                            "kind": "linky_tic",
                            "tic": {"port": "/dev/ttyUSB0", "mode": "standard"},
                        }
                    ],
                }
            )
        )
        assert config_mod.load(path).devices[0].options == {
            "port": "/dev/ttyUSB0",
            "mode": "standard",
        }
        path.write_text(
            json.dumps(
                {
                    "cloud_url": "https://api.h2s.example",
                    "db_url": "postgresql://x@/y",
                    "devices": [
                        {
                            "device_id": 7,
                            "kind": "linky_tic",
                            "tic": {"port": "/etc/passwd", "mode": "standard"},
                        }
                    ],
                }
            )
        )
        with pytest.raises(ValueError):
            config_mod.load(path)


class ModbusSimulator(socketserver.ThreadingTCPServer):
    """Serveur Modbus TCP simulé : fonctions 3 et 4, exception 02 sur un registre absent, exception 01 sur une autre fonction."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, registers: dict[int, int], unit: int = 1) -> None:
        self.registers = registers
        self.unit = unit
        self.requests: list[bytes] = []
        super().__init__(("127.0.0.1", 0), ModbusHandler)


class ModbusHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: ModbusSimulator = self.server  # type: ignore[assignment]
        header = self.request.recv(7)
        if len(header) < 7:
            return
        tid, proto, length, unit = struct.unpack(">HHHB", header)
        pdu = self.request.recv(length - 1)
        server.requests.append(header + pdu)
        answer = respond(server.registers, pdu)
        self.request.sendall(struct.pack(">HHHB", tid, proto, len(answer) + 1, unit) + answer)


def respond(registers: dict[int, int], pdu: bytes) -> bytes:
    fc = pdu[0]
    if fc not in (3, 4):
        return bytes([fc | 0x80, 1])
    address, count = struct.unpack(">HH", pdu[1:5])
    if any(a not in registers for a in range(address, address + count)):
        return bytes([fc | 0x80, 2])
    return bytes([fc, 2 * count]) + b"".join(struct.pack(">H", registers[a]) for a in range(address, address + count))


def words(fmt: str, value) -> list[int]:
    raw = struct.pack(modbus.FORMATS[fmt][1], value)
    return [struct.unpack(">H", raw[i : i + 2])[0] for i in range(0, len(raw), 2)]


def pm5000_registers(power_kw=1.5, pf=1.2, energy_wh=12_345_678) -> dict[int, int]:
    regs: dict[int, int] = {}
    for address, fmt, value in (
        (3009, "float32", 6.5),
        (3035, "float32", 231.0),
        (3059, "float32", power_kw),
        (3083, "float32", pf),
        (3203, "int64", energy_wh),
    ):
        for i, w in enumerate(words(fmt, value)):
            regs[address + i] = w
    return regs


class TestModbus:
    def test_vecteurs_de_la_specification(self):
        assert modbus.crc16(bytes.fromhex("010300000001")) == 0x0A84, "trame RTU 01 03 00 00 00 01 84 0A"
        assert modbus.crc16(bytes.fromhex("01030000000A")) == 0xCDC5, "trame RTU 01 03 00 00 00 0A C5 CD"
        reg = modbus.Register("current_a", 0x6B, "uint16")
        assert modbus.decode(reg, [0x022B]) == 555.0, "Modbus Application Protocol §6.3 : registre 108 = 0x022B"
        assert modbus.decode(
            modbus.Register("power_factor", 0, "float32", decode="pf4q"),
            words("float32", 1.2),
        ) == pytest.approx(0.8)
        assert modbus.decode(
            modbus.Register("power_factor", 0, "float32", decode="pf4q"),
            words("float32", -1.1),
        ) == pytest.approx(-0.9)
        assert (
            modbus.decode(
                modbus.Register("energy_total_wh", 0, "uint32", word_order="little"),
                [0x5678, 0x1234],
            )
            == 0x12345678
        )
        with pytest.raises(ValueError):
            modbus.Register("mot_de_passe", 0)

    def test_centrale_pm5000_lue_par_modbus_tcp_sur_serveur_simule(self):
        server = ModbusSimulator(pm5000_registers())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            device = modbus.device_from_config(
                12,
                {
                    "transport": "tcp",
                    "host": "127.0.0.1",
                    "port": server.server_address[1],
                    "unit_id": 1,
                    "profile": "pm5000",
                },
            )
            readings = device.poll(T0)
            values = {r.data_type: r.value for r in readings}
            assert values["power_w"] == pytest.approx(1500.0) and values["energy_total_wh"] == 12_345_678
            assert (
                values["current_a"] == 6.5
                and values["voltage_v"] == 231.0
                and values["power_factor"] == pytest.approx(0.8)
            )
            assert all(r.device_key == "device:12" for r in readings)
            assert server.requests[0][7:] == bytes.fromhex("030BC10002"), (
                "fonction 3, registre 3010 (adresse 3009), 2 mots"
            )
            missing = modbus.device_from_config(
                12,
                {
                    "host": "127.0.0.1",
                    "port": server.server_address[1],
                    "registers": [{"data_type": "power_w", "address": 40000, "fmt": "float32"}],
                },
            )
            with pytest.raises(modbus.ModbusError, match="adresse de registre illégale"):
                missing.poll(T0)
            server.registers.update(pm5000_registers(power_kw=5000.0))
            with pytest.raises(parsers.ParseError):
                device.poll(T0)
        finally:
            server.shutdown()
            server.server_close()

    def test_esclave_rtu_sur_port_serie_simule(self):
        master, slave = os.openpty()
        registers = pm5000_registers()

        def slave_loop():
            buf = b""
            while True:
                try:
                    buf += os.read(master, 64)
                except OSError:
                    return
                if len(buf) >= 8:
                    adu, buf = buf[:8], buf[8:]
                    assert modbus.crc16(adu[:-2]) == struct.unpack("<H", adu[-2:])[0]
                    answer = bytes([adu[0]]) + respond(registers, adu[1:6])
                    os.write(master, answer + struct.pack("<H", modbus.crc16(answer)))

        threading.Thread(target=slave_loop, daemon=True).start()
        try:
            device = modbus.device_from_config(
                13,
                {
                    "transport": "rtu",
                    "serial_port": os.ttyname(slave),
                    "baudrate": 19200,
                    "parity": "E",
                    "unit_id": 17,
                    "profile": "pm5000",
                },
            )
            values = {r.data_type: r.value for r in device.poll(T0)}
            assert values["energy_total_wh"] == 12_345_678 and values["power_w"] == pytest.approx(1500.0)
        finally:
            os.close(slave)
            os.close(master)

    def test_une_adresse_mac_nest_pas_un_identifiant_mqtt(self, tmp_path):
        """Constaté en clientèle le 02/10/2026 : une MAC saisie en mqtt_id donnait un abonnement sur un sujet que la
        prise ne publiera jamais — aucune mesure, aucune erreur, et cinq minutes plus tard une alerte « hors ligne »
        qui ne disait pas pourquoi. L'agent refuse maintenant de démarrer, et dit quoi mettre à la place."""
        path = tmp_path / "agent.json"
        base = {"cloud_url": "https://api.h2s.example", "db_url": "postgresql://x@/y"}

        def ecrire(mqtt_id: str) -> None:
            path.write_text(
                json.dumps(
                    {
                        **base,
                        "devices": [{"device_id": 8, "kind": "shelly_gen2", "mqtt_id": mqtt_id, "role": "consumption"}],
                    }
                )
            )

        # Les trois formes d'une MAC : nue (comme l'affiche l'interface Shelly), avec tirets, avec deux-points.
        for mac in ("A8032ABCDEF1", "a8032abcdef1", "A8-03-2A-BC-DE-F1", "A8:03:2A:BC:DE:F1"):
            ecrire(mac)
            with pytest.raises(ValueError) as exc:
                config_mod.load(path)
            assert "équipement 8" in str(exc.value), mac
        # Et le message de la forme nue PROPOSE la valeur probable, pour ne pas laisser chercher.
        ecrire("A8032ABCDEF1")
        with pytest.raises(ValueError, match="shellyplusplugs-a8032abcdef1"):
            config_mod.load(path)
        # L'identifiant réel passe, évidemment.
        ecrire("shellyplusplugs-a8032abcdef1")
        assert config_mod.load(path).devices[0].mqtt_id == "shellyplusplugs-a8032abcdef1"

    def test_configuration_invalide_refusee(self, tmp_path):
        path = tmp_path / "agent.json"
        base = {"cloud_url": "https://api.h2s.example", "db_url": "postgresql://x@/y"}
        path.write_text(
            json.dumps(
                {
                    **base,
                    "devices": [
                        {
                            "device_id": 12,
                            "kind": "powerlogic_modbus",
                            "modbus": {"host": "10.0.0.5", "profile": "pm5000"},
                        }
                    ],
                }
            )
        )
        assert config_mod.load(path).devices[0].kind == "powerlogic_modbus"
        for bad in (
            {"host": "10.0.0.5"},
            {"host": "10.0.0.5", "profile": "pm9999"},
            {"transport": "udp", "host": "x", "profile": "pm5000"},
            {"host": "10.0.0.5", "unit_id": 300, "profile": "pm5000"},
        ):
            path.write_text(
                json.dumps(
                    {
                        **base,
                        "devices": [
                            {
                                "device_id": 12,
                                "kind": "powerlogic_modbus",
                                "modbus": bad,
                            }
                        ],
                    }
                )
            )
            with pytest.raises(ValueError):
                config_mod.load(path)
