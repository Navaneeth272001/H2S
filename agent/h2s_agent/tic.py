"""Téléinformation client (TIC) des compteurs Linky, lue localement sur le port série (E172, E188 ; §8 B9).

Normes Enedis : mode **historique** (Enedis-NOI-CPT_02E : 1 200 bauds, 7 bits, parité paire, séparateur espace) et mode
**standard** (Enedis-NOI-CPT_54E : 9 600 bauds, 7 bits, parité paire, séparateur tabulation, groupes horodatés). Une trame
va de STX (0x02) à ETX (0x03) ; chaque groupe de LF (0x0A) à CR (0x0D) ; EOT (0x04) interrompt une trame.

Somme de contrôle d'un groupe : ``(somme des octets & 0x3F) + 0x20`` —
* historique : de l'étiquette à la donnée, sans le séparateur qui précède la somme ;
* standard : de l'étiquette au séparateur qui précède la somme **inclus**.
Un groupe à la somme fausse est écarté et compté ; il n'est jamais lu.

Relevés produits (``parsers.Reading``, source ``device``) :
* index d'énergie soutirée (Wh) → ``energy_total_wh`` : ``EAST`` (standard), ``BASE`` ou somme des index tarifaires
  (historique) ; la fenêtre de 5 minutes en tire l'énergie consommée, comme pour une prise ;
* ``IRMS1`` / ``IINST`` → ``current_a`` ; ``URMS1`` → ``voltage_v`` ; ``SINSTS`` / ``PAPP`` (VA) → ``apparent_power_va`` ;
  ``EAIT`` → ``energy_injected_total_wh`` et ``SINSTI`` → ``apparent_power_injected_va`` (production), gardés en local.

Le Linky ne publie pas de puissance active instantanée : aucune n'est déduite de la puissance apparente.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from . import parsers

LOG = logging.getLogger("h2s_agent.tic")

STX, ETX, EOT, LF, CR, SP, HT = 0x02, 0x03, 0x04, 0x0A, 0x0D, 0x20, 0x09
MODES = {"historique": (1200, SP), "standard": (9600, HT)}
HISTORIC_INDEXES = (
    "BASE",
    "HCHC",
    "HCHP",
    "EJPHN",
    "EJPHPM",
    "BBRHCJB",
    "BBRHPJB",
    "BBRHCJW",
    "BBRHPJW",
    "BBRHCJR",
    "BBRHPJR",
)
#: Taille maximale d'une trame : au-delà, la ligne est bruitée ou ce n'est pas une TIC (protection mémoire).
MAX_FRAME = 4096


class TicError(ValueError):
    pass


def checksum(data: bytes) -> int:
    return (sum(data) & 0x3F) + 0x20


@dataclass
class Frame:
    """Groupes valides d'une trame : étiquette → (donnée, horodate ou ``None``) ; groupes écartés comptés."""

    groups: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    rejected: int = 0


def decode_group(raw: bytes, mode: str) -> tuple[str, str, str | None]:
    """Un groupe sans LF ni CR. Lève ``TicError`` si la somme de contrôle ou la forme est fausse."""
    if mode not in MODES:
        raise TicError(f"mode TIC inconnu : {mode}")
    sep = MODES[mode][1]
    if len(raw) < 4 or raw[-2] != sep:
        raise TicError("groupe mal formé")
    expected = raw[-1]
    if mode == "standard":
        body = raw[:-1]  # séparateur final inclus
        parts = raw[:-2].split(bytes([sep]))
    else:
        body = raw[:-2]
        parts = raw[:-2].split(bytes([sep]), 1)
    if checksum(body) != expected:
        raise TicError("somme de contrôle fausse")
    try:
        text = [p.decode("ascii") for p in parts]
    except UnicodeDecodeError as exc:
        raise TicError("caractère non ASCII") from exc
    if mode == "standard" and len(text) == 3:
        label, horodate, value = text
        return label, value, horodate
    if len(text) != 2:
        raise TicError("groupe mal formé")
    label, value = text
    return label, value, None


def decode_frame(raw: bytes, mode: str) -> Frame:
    """Trame entre STX et ETX (bornes facultatives)."""
    body = raw.strip(bytes([STX, ETX]))
    frame = Frame()
    for chunk in body.split(bytes([LF])):
        chunk = chunk.rstrip(bytes([CR]))
        if not chunk:
            continue
        try:
            label, value, horodate = decode_group(chunk, mode)
        except TicError:
            frame.rejected += 1
            continue
        frame.groups[label] = (value, horodate)
    return frame


def _int(frame: Frame, label: str) -> float | None:
    raw = frame.groups.get(label)
    if raw is None:
        return None
    try:
        return float(int(raw[0].strip()))
    except ValueError:
        return None


def meter_id(frame: Frame) -> str | None:
    raw = frame.groups.get("ADSC") or frame.groups.get("ADCO")
    return raw[0].strip() if raw else None


def readings(
    device_id: int,
    frame: Frame,
    mode: str,
    received: dt.datetime,
    clock_synced: bool = True,
    expected_meter: str | None = None,
) -> list[parsers.Reading]:
    """Relevés typés d'une trame ; ``expected_meter`` : adresse du compteur attendu sur ce port (autre compteur refusé)."""
    found = meter_id(frame)
    if expected_meter is not None and found != expected_meter:
        raise parsers.ParseError(f"trame d'un autre compteur ({found}) sur le port de {expected_meter}")
    if mode == "standard":
        values = {
            "energy_total_wh": _int(frame, "EAST"),
            "energy_injected_total_wh": _int(frame, "EAIT"),
            "current_a": _int(frame, "IRMS1"),
            "voltage_v": _int(frame, "URMS1"),
            "apparent_power_va": _int(frame, "SINSTS"),
            "apparent_power_injected_va": _int(frame, "SINSTI"),
        }
    else:
        indexes = [v for v in (_int(frame, k) for k in HISTORIC_INDEXES) if v is not None]
        values = {
            "energy_total_wh": sum(indexes) if indexes else None,
            "current_a": _int(frame, "IINST"),
            "apparent_power_va": _int(frame, "PAPP"),
        }
    quality = "OK" if clock_synced else "ESTIMATED"
    return parsers._readings("device", device_id, values, received, quality)


class FrameSplitter:
    """Découpe un flux d'octets en trames STX…ETX ; EOT, trame trop longue ou octets hors trame : ignorés."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._inside = False

    def feed(self, data: bytes) -> list[bytes]:
        frames = []
        for b in data:
            b &= 0x7F  # 7 bits de données : le bit de parité éventuellement laissé par le pilote est retiré
            if b == STX:
                self._buf = bytearray([STX])
                self._inside = True
            elif not self._inside:
                continue
            elif b == EOT:
                self._inside = False
                self._buf = bytearray()
            elif b == ETX:
                self._buf.append(ETX)
                frames.append(bytes(self._buf))
                self._inside = False
                self._buf = bytearray()
            else:
                self._buf.append(b)
                if len(self._buf) > MAX_FRAME:
                    self._inside = False
                    self._buf = bytearray()
        return frames


def open_serial(port: str, mode: str) -> int:
    """Port série en 7E1 brut à la vitesse du mode (1 200 ou 9 600 bauds)."""
    import termios

    baud = {1200: termios.B1200, 9600: termios.B9600}[MODES[mode][0]]
    fd = os.open(port, os.O_RDONLY | os.O_NOCTTY)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = termios.IGNPAR  # iflag
        attrs[1] = 0  # oflag
        attrs[2] = termios.CS7 | termios.PARENB | termios.CREAD | termios.CLOCAL  # cflag : 7 bits, parité paire
        attrs[3] = 0  # lflag : brut
        attrs[4] = attrs[5] = baud
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 10  # lecture bloquante une seconde au plus : l'arrêt est pris en compte
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:
        os.close(fd)
        raise
    return fd


class TicReader:
    """Fil de lecture d'un port TIC : chaque trame valide devient des relevés remis à ``on_readings``."""

    def __init__(
        self,
        device_id: int,
        port: str,
        mode: str,
        on_readings: Callable[[list[parsers.Reading], dt.datetime], int],
        now: Callable[[], dt.datetime],
        clock_synced: Callable[[], bool],
        expected_meter: str | None = None,
    ) -> None:
        if mode not in MODES:
            raise TicError(f"mode TIC inconnu : {mode}")
        self.device_id = device_id
        self.port = port
        self.mode = mode
        self._on = on_readings
        self._now = now
        self._synced = clock_synced
        self._meter = expected_meter
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.frames = 0
        self.rejected_groups = 0
        self.rejected_frames = 0

    def handle(self, raw: bytes) -> int:
        frame = decode_frame(raw, self.mode)
        self.rejected_groups += frame.rejected
        received = self._now()
        try:
            values = readings(self.device_id, frame, self.mode, received, self._synced(), self._meter)
        except parsers.ParseError as exc:
            self.rejected_frames += 1
            LOG.warning("trame TIC écartée sur %s : %s", self.port, exc)
            return 0
        self.frames += 1
        return self._on(values, received)

    def run(self) -> None:
        splitter = FrameSplitter()
        while not self._stop.is_set():
            try:
                fd = open_serial(self.port, self.mode)
            except OSError as exc:
                LOG.warning("port TIC %s indisponible : %s", self.port, exc)
                self._stop.wait(10)
                continue
            try:
                while not self._stop.is_set():
                    data = os.read(fd, 512)
                    for raw in splitter.feed(data):
                        try:
                            self.handle(raw)
                        except Exception:
                            LOG.exception("trame TIC non traitée")
            except OSError as exc:
                LOG.warning("lecture TIC %s interrompue : %s", self.port, exc)
            finally:
                os.close(fd)

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name=f"tic-{self.device_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
