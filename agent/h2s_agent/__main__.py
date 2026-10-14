"""Point d'entrée : ``python -m h2s_agent {migrate|enroll|run} --config agent.json``."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
import uuid
from pathlib import Path

from . import __version__, enrollment, integrity
from . import config as config_mod
from .clock import Clock
from .cloud import CloudClient, CloudRefused, CloudUnavailable
from .cloud_mqtt import CloudMqtt
from .crypto import KeyStore, SoftwareKeyStore
from .local_api import LocalApi, ensure_token
from .mqtt import MqttBridge
from .orchestrator import Agent, load_identity, save_identity
from .storage import Database, migrate

logger = logging.getLogger(__name__)


def _routes(agent: Agent) -> dict:
    def config_view(_body):
        c = agent.config
        return 200, {
            "cloud_url": c.cloud_url,
            "devices": [d.__dict__ for d in c.devices],
            "batteries": [b.__dict__ for b in c.batteries],
            "weather_topic": c.weather_topic,
            "buffer_max": c.buffer_max,
        }

    def config_update(body):
        # Seul le mode manuel se règle à chaud ; le reste passe par le fichier et un redémarrage (traçable).
        return 409, {
            "code": "restart_required",
            "detail": "modifier agent.json puis redémarrer le service h2s-agent",
        }

    def decisions(_body):
        with agent.db.tx() as cur:
            cur.execute(
                "SELECT decision_id, stock_id, action, power_w, source, execution_status, rejection_rule, rejection_reason, executed_at"
                " FROM local_decisions ORDER BY created_at DESC LIMIT 50"
            )
            return 200, cur.fetchall()

    def manual(body):
        if agent.execution_blocked:
            return 409, {
                "code": "execution_blocked",
                "detail": "intégrité du paquet non vérifiée : aucun ordre exécuté, manuel compris",
            }
        duration = int(body.get("duration_s", 600))
        if not 1 <= duration <= 86_400:
            return 400, {
                "code": "invalid_duration",
                "detail": "durée attendue entre 1 et 86 400 secondes",
            }
        return 200, agent.decisions.manual(
            int(body["stock_id"]),
            str(body["action"]).upper(),
            float(body.get("power_w", 0)),
            duration,
        )

    def override(body):
        until = agent.decisions.set_manual(bool(body.get("enabled")))
        return 200, {
            "manual_mode": agent.decisions.manual_mode,
            "until": until.isoformat() if until else None,
        }

    def flush(_body):
        return 200, {"sent": agent.sync()}

    return {
        ("GET", "/api/agent/health"): lambda _b: agent.health(),
        ("GET", "/api/agent/status"): lambda _b: (200, agent.status()),
        ("GET", "/api/agent/config"): config_view,
        ("PUT", "/api/agent/config"): config_update,
        ("GET", "/api/agent/buffer"): lambda _b: (
            200,
            {"size": agent.buffer.size(), "by_priority": agent.buffer.by_priority()},
        ),
        ("POST", "/api/agent/buffer/flush"): flush,
        ("GET", "/api/agent/decisions"): decisions,
        ("POST", "/api/agent/decisions"): manual,
        ("POST", "/api/agent/override"): override,
        ("GET", "/api/agent/tpm"): lambda _b: (200, agent.tpm_view()),
    }


def key_store(conf: config_mod.AgentConfig) -> KeyStore:
    """Magasin de la clé d'identité choisi dans agent.json : jamais de repli logiciel quand le TPM est configuré et absent."""
    if conf.key_store == "tpm2":
        from .tpm import Tpm2KeyStore

        return Tpm2KeyStore(conf.tpm_tcti)
    return SoftwareKeyStore(conf.key_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="h2s_agent", description="Agent local H2S")
    parser.add_argument("command", choices=["migrate", "enroll", "run", "version"])
    parser.add_argument("--config", default="/etc/h2s-agent/agent.json")
    parser.add_argument(
        "--code",
        help="code d'enrôlement (déconseillé : visible de ps et de l'historique ; préférer la saisie ou --code-file)",
    )
    parser.add_argument("--code-file", help="fichier contenant le code d'enrôlement")
    parser.add_argument(
        "--allow-unsigned-package",
        action="store_true",
        help="développement seulement : exécuter sans manifeste signé",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "version":
        print(__version__)
        return 0
    conf = config_mod.load(args.config)
    db = Database(conf.db_url)
    applied = migrate(db)
    if applied:
        logger.info("migrations appliquées : %s", ", ".join(applied))
    if args.command == "migrate":
        return 0
    clock = Clock()
    try:
        keys = key_store(conf)
    except RuntimeError as exc:  # TpmError : TPM configuré mais absent ou en erreur
        print(f"magasin de clés {conf.key_store} indisponible : {exc}", file=sys.stderr)
        return 3
    cloud = CloudClient(conf.cloud_url, keys, clock)
    if args.command == "enroll":
        if args.code_file:
            args.code = Path(args.code_file).read_text(encoding="utf-8").strip()
        elif not args.code:
            args.code = getpass.getpass("Code d'enrôlement : ")
        if not args.code:
            parser.error("code d'enrôlement attendu")
        if load_identity(db) is not None:
            print(
                "agent déjà enrôlé ; pour un nouveau site, réinitialiser la base et la clé",
                file=sys.stderr,
            )
            return 2
        try:
            registered = enrollment.enroll(cloud, keys, args.code, __version__, str(uuid.uuid4()))
        except (CloudRefused, CloudUnavailable, ValueError) as exc:
            print(f"enrôlement refusé : {exc}", file=sys.stderr)
            return 1
        ident = save_identity(db, conf.cloud_url, registered)
        tpm_state = registered.get("tpm_attestation")
        print(
            f"agent {ident.agent_uuid} enrôlé sur le site {ident.site_id}"
            + (f" (TPM : {tpm_state})" if tpm_state else "")
        )
        return 0
    ident = load_identity(db)
    if ident is None:
        print(
            "agent non enrôlé : python -m h2s_agent enroll --config … (code saisi au clavier ou --code-file)",
            file=sys.stderr,
        )
        return 2
    bridge = MqttBridge(conf.mqtt, f"h2s-agent-{ident.site_id}", [], lambda t, p: None)
    # Canal temps réel vers le cloud (lot 5) : ouvert seulement s'il est configuré ; sinon l'agent sonde comme avant.
    cloud_mqtt = CloudMqtt(conf.cloud_mqtt, keys, ident.agent_uuid, clock) if conf.cloud_mqtt.enabled else None
    # Paquet chiffré chargé par l'amorce (E177) : le manifeste signé couvre l'amorce et le paquet chiffré, pas des sources absentes.
    boot = sys.modules.get("h2s_boot.package")
    app_dir = getattr(boot, "APP_DIR", None)
    agent = Agent(
        conf,
        db,
        cloud,
        bridge,
        clock,
        require_signed_package=not args.allow_unsigned_package,
        integrity_root=app_dir if app_dir is not None else integrity.PACKAGE,
        cloud_mqtt=cloud_mqtt,
    )
    if args.allow_unsigned_package:
        # Développement : le paquet n'est pas signé, l'environnement n'est pas vérifié non plus.
        agent._site_packages = None
    bridge.bind(agent.collector.topics(), agent.collector.on_message)
    bridge.start()
    api = LocalApi(conf.local_api_port, ensure_token(conf.token_path), _routes(agent))
    api.start()
    try:
        agent.run_forever()
    finally:
        api.stop()
        bridge.stop()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
