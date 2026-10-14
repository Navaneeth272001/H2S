"""Contexte TLS des connexions sortantes de l'agent (CDC §3.3 « MQTT (EMQX) avec QoS 1, TLS 1.3 », C-2, E186).

Un seul point de réglage pour le client du cloud (HTTPS) et le pont MQTT : TLS 1.3 au minimum, certificat du serveur vérifié et
nom d'hôte contrôlé. Un serveur limité à TLS 1.2 est refusé à la poignée de main ; aucune version plus ancienne n'est rattrapée.
"""

from __future__ import annotations

import ssl

MINIMUM_VERSION = ssl.TLSVersion.TLSv1_3


def client_context(ca_file: str | None = None) -> ssl.SSLContext:
    """Contexte client TLS 1.3 minimum ; ``ca_file`` : autorité propre au déploiement, sinon les autorités du système."""
    ctx = ssl.create_default_context(cafile=ca_file)
    ctx.minimum_version = MINIMUM_VERSION
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx
