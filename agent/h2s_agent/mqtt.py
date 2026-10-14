"""Pont vers le broker MQTT local (Mosquitto, D255) : abonnements de collecte et publication des commandes (QoS 1).

Le broker exige authentification et ACL (guide de déploiement) ; le mot de passe est lu dans un fichier 0600. La
connexion est reprise automatiquement ; une publication impossible renvoie ``False`` et l'ordre est compté en échec. En TLS,
TLS 1.3 au minimum et certificat vérifié (C-2).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

import paho.mqtt.client as paho

from . import tls
from .config import MqttConf

LOG = logging.getLogger("h2s_agent.mqtt")


class MqttBridge:
    def __init__(
        self,
        conf: MqttConf,
        client_id: str,
        topics: list[str],
        on_message: Callable[[str, bytes], None],
    ) -> None:
        self._topics = topics
        self._on_message = on_message
        self._connected = threading.Event()
        self._client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=True)
        if conf.username:
            password = Path(conf.password_file).read_text(encoding="utf-8").strip() if conf.password_file else None
            self._client.username_pw_set(conf.username, password)
        if conf.tls:
            self._client.tls_set_context(tls.client_context(conf.ca_file))
        self._client.on_connect = self._connected_cb
        self._client.on_disconnect = self._disconnected_cb
        self._client.on_message = self._message_cb
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._host, self._port = conf.host, conf.port

    def bind(self, topics: list[str], on_message: Callable[[str, bytes], None]) -> None:
        """Abonnements et traitement fournis une fois l'agent construit (avant ``start``)."""
        self._topics = list(topics)
        self._on_message = on_message

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        self._client.connect_async(self._host, self._port, keepalive=60)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def wait(self, timeout: float) -> bool:
        return self._connected.wait(timeout)

    def _connected_cb(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            LOG.error("connexion MQTT refusée : %s", reason_code)
            return
        for t in self._topics:
            client.subscribe(t, qos=1)
        self._connected.set()
        LOG.info("MQTT connecté, %d abonnements", len(self._topics))

    def _disconnected_cb(self, client, userdata, flags, reason_code, properties) -> None:
        self._connected.clear()
        LOG.warning("MQTT déconnecté : %s", reason_code)

    def _message_cb(self, client, userdata, msg) -> None:
        try:
            self._on_message(msg.topic, msg.payload)
        except Exception:
            LOG.exception("traitement du message %s en échec", msg.topic)

    def publish(self, topic: str, payload: str, qos: int = 1, retain: bool = False) -> bool:
        if not self.connected:
            return False
        info = self._client.publish(topic, payload, qos=qos, retain=retain)
        try:
            info.wait_for_publish(timeout=2)
        except (RuntimeError, ValueError):
            return False
        return info.is_published()
