"""Lecture Modbus des centrales de mesure PowerLogic (E172 ; §2.18 collecte, spécification 18 §4.1.2 « MQTT / Modbus »).

Client Modbus minimal sans dépendance : TCP (MBAP, port 502) ou RTU (port série, CRC-16), fonctions 3 (registres de
maintien) et 4 (registres d'entrée). La table de registres est **configurable** ; le profil ``pm5000`` reprend la table
publique des PowerLogic PM5000 (adresses des registres de la documentation Schneider moins un) et reste à confirmer sur la
référence exacte installée chez le client : aucune valeur n'est lue d'un registre qui n'est pas déclaré.

Lecture toutes les 60 secondes par l'orchestrateur ; chaque grandeur est validée sur sa plage physique comme un message MQTT.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import socket
import struct
import threading
from dataclasses import dataclass

from . import parsers

FORMATS = {
    "int16": (1, ">h"),
    "uint16": (1, ">H"),
    "int32": (2, ">i"),
    "uint32": (2, ">I"),
    "float32": (2, ">f"),
    "int64": (4, ">q"),
    "uint64": (4, ">Q"),
    "float64": (4, ">d"),
}
FUNCTIONS = {"holding": 3, "input": 4}
EXCEPTIONS = {
    1: "fonction illégale",
    2: "adresse de registre illégale",
    3: "valeur illégale",
    4: "défaillance de l'esclave",
    6: "esclave occupé",
}


class ModbusError(OSError):
    pass


@dataclass(frozen=True)
class Register:
    """Grandeur lue : type de relevé H2S, adresse Modbus (base 0), format, facteur, ordre des mots, décodage particulier."""

    data_type: str
    address: int
    fmt: str = "float32"
    kind: str = "holding"
    scale: float = 1.0
    word_order: str = "big"
    decode: str | None = None  # "pf4q" : facteur de puissance à quatre quadrants des PowerLogic

    def __post_init__(self) -> None:
        if self.data_type not in parsers.RANGES:
            raise ValueError(f"registre : type de relevé inconnu {self.data_type!r}")
        if self.fmt not in FORMATS or self.kind not in FUNCTIONS or self.word_order not in ("big", "little"):
            raise ValueError(f"registre {self.data_type} : format, fonction ou ordre des mots invalide")
        if not 0 <= self.address <= 65535 - FORMATS[self.fmt][0] + 1 or not math.isfinite(self.scale):
            raise ValueError(f"registre {self.data_type} : adresse ou facteur invalide")

    @property
    def count(self) -> int:
        return FORMATS[self.fmt][0]


#: PowerLogic PM5000 (PM5100/5300/5500), adresses Modbus = registre documenté − 1. À confirmer sur la référence installée.
PROFILES: dict[str, tuple[Register, ...]] = {
    "pm5000": (
        Register("current_a", 3009, "float32"),  # 3010 Current Avg (A)
        Register("voltage_v", 3035, "float32"),  # 3036 Voltage L-N Avg (V)
        Register("power_w", 3059, "float32", scale=1000.0),  # 3060 Active Power Total (kW)
        Register("power_factor", 3083, "float32", decode="pf4q"),  # 3084 Power Factor Total (4 quadrants)
        Register("energy_total_wh", 3203, "int64"),  # 3204 Active Energy Delivered (Into Load) (Wh)
    ),
}


def crc16(frame: bytes) -> int:
    """CRC-16 Modbus (polynôme 0xA001, valeur initiale 0xFFFF), transmis poids faible en tête."""
    crc = 0xFFFF
    for b in frame:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def pf4q(value: float) -> float:
    """Facteur de puissance PowerLogic codé de −2 à 2 (quadrant) ramené à −1…1."""
    if value > 1:
        return 2 - value
    if value < -1:
        return -2 - value
    return value


def decode(register: Register, words: list[int]) -> float:
    if register.word_order == "little":
        words = list(reversed(words))
    raw = b"".join(struct.pack(">H", w) for w in words)
    value = float(struct.unpack(FORMATS[register.fmt][1], raw)[0])
    if register.decode == "pf4q":
        value = pf4q(value)
    return value * register.scale


class TcpTransport:
    def __init__(self, host: str, port: int = 502, timeout: float = 3.0) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self._tid = 0
        self._lock = threading.Lock()

    def request(self, unit: int, pdu: bytes) -> bytes:
        with self._lock:
            self._tid = (self._tid + 1) & 0xFFFF
            tid = self._tid
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
                s.sendall(struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu)
                header = _recv(s, 7)
                rtid, proto, length, runit = struct.unpack(">HHHB", header)
                if rtid != tid or proto != 0 or runit != unit or not 2 <= length <= 254:
                    raise ModbusError("réponse Modbus TCP incohérente")
                return _recv(s, length - 1)


def _recv(s: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = s.recv(n - len(data))
        if not chunk:
            raise ModbusError("connexion Modbus fermée")
        data += chunk
    return data


class RtuTransport:
    """Modbus RTU sur port série (8 bits, parité configurable, 1 bit d'arrêt)."""

    def __init__(self, port: str, baudrate: int = 19200, parity: str = "E", timeout: float = 1.0) -> None:
        self.port, self.baudrate, self.parity, self.timeout = (
            port,
            baudrate,
            parity,
            timeout,
        )
        self._lock = threading.Lock()
        self._fd: int | None = None

    def _open(self) -> int:
        import termios

        speeds = {
            9600: termios.B9600,
            19200: termios.B19200,
            38400: termios.B38400,
            115200: termios.B115200,
        }
        if self.baudrate not in speeds or self.parity not in ("N", "E", "O"):
            raise ModbusError("RTU : vitesse ou parité non prise en charge")
        fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY)
        attrs = termios.tcgetattr(fd)
        cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
        if self.parity != "N":
            cflag |= termios.PARENB | (termios.PARODD if self.parity == "O" else 0)
        attrs[0], attrs[1], attrs[2], attrs[3] = termios.IGNPAR, 0, cflag, 0
        attrs[4] = attrs[5] = speeds[self.baudrate]
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = max(1, int(self.timeout * 10))
        try:
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except termios.error:
            os.close(fd)
            raise
        return fd

    def request(self, unit: int, pdu: bytes) -> bytes:
        """Port ouvert une fois et gardé : reconfigurer une ligne série à chaque trame est lent et refusé par certains pilotes."""
        import termios

        adu = bytes([unit]) + pdu
        adu += struct.pack("<H", crc16(adu))
        with self._lock:
            if self._fd is None:
                self._fd = self._open()
            fd = self._fd
            try:
                termios.tcflush(fd, termios.TCIFLUSH)
                os.write(fd, adu)
                head = self._read(fd, 3)
                frame = head + self._read(fd, 2 if head[1] & 0x80 else head[2] + 2)
            except (OSError, termios.error) as exc:
                self.close()
                raise ModbusError(f"RTU : {exc}") from exc
        if crc16(frame[:-2]) != struct.unpack("<H", frame[-2:])[0]:
            raise ModbusError("RTU : CRC faux")
        if frame[0] != unit:
            raise ModbusError("RTU : réponse d'un autre esclave")
        return frame[1:-2]

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    def _read(self, fd: int, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = os.read(fd, n - len(data))
            if not chunk:
                raise ModbusError("RTU : délai de réponse dépassé")
            data += chunk
        return data


class ModbusDevice:
    """Centrale de mesure lue par registres ; renvoie des relevés validés."""

    def __init__(self, device_id: int, transport, unit: int, registers: tuple[Register, ...]) -> None:
        if not registers:
            raise ValueError("centrale Modbus sans registre déclaré")
        self.device_id, self.transport, self.unit, self.registers = (
            device_id,
            transport,
            unit,
            registers,
        )

    def read_words(self, register: Register) -> list[int]:
        pdu = struct.pack(">BHH", FUNCTIONS[register.kind], register.address, register.count)
        resp = self.transport.request(self.unit, pdu)
        if resp[0] & 0x80:
            raise ModbusError(f"exception Modbus : {EXCEPTIONS.get(resp[1], resp[1])} (registre {register.address})")
        if resp[0] != FUNCTIONS[register.kind] or resp[1] != 2 * register.count or len(resp) != 2 + 2 * register.count:
            raise ModbusError("réponse Modbus de longueur inattendue")
        return list(struct.unpack(f">{register.count}H", resp[2:]))

    def poll(self, received: dt.datetime, clock_synced: bool = True) -> list[parsers.Reading]:
        values: dict[str, float | None] = {}
        for register in self.registers:
            value = decode(register, self.read_words(register))
            values[register.data_type] = value if math.isfinite(value) else None
        return parsers._readings(
            "device",
            self.device_id,
            values,
            received,
            "OK" if clock_synced else "ESTIMATED",
        )


def registers_from_config(options: dict) -> tuple[Register, ...]:
    if "registers" in options:
        return tuple(Register(**r) for r in options["registers"])
    profile = options.get("profile")
    if profile not in PROFILES:
        raise ValueError(f"centrale Modbus : profil connu ({', '.join(PROFILES)}) ou table « registers » attendu")
    return PROFILES[profile]


def device_from_config(device_id: int, options: dict) -> ModbusDevice:
    transport = options.get("transport", "tcp")
    if transport == "tcp":
        t = TcpTransport(
            str(options["host"]),
            int(options.get("port", 502)),
            float(options.get("timeout_s", 3.0)),
        )
    elif transport == "rtu":
        t = RtuTransport(
            str(options["serial_port"]),
            int(options.get("baudrate", 19200)),
            str(options.get("parity", "E")),
            float(options.get("timeout_s", 1.0)),
        )
    else:
        raise ValueError("centrale Modbus : transport tcp ou rtu attendu")
    unit = int(options.get("unit_id", 1))
    if not 0 <= unit <= 247:
        raise ValueError("centrale Modbus : unit_id de 0 à 247")
    return ModbusDevice(device_id, t, unit, registers_from_config(options))
