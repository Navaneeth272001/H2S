"""Amorce de l'agent H2S (E177) : seul code en clair d'une installation à paquet chiffré.

Elle vérifie la signature du paquet ``h2s_agent.enc``, ouvre sa clé AES par la clé de code du boîtier (TPM 2.0 ou fichier
0600), déchiffre le paquet **en mémoire** et le fait importer depuis la mémoire, puis lance l'agent. Aucun fichier source ni
bytecode de ``h2s_agent`` n'est écrit sur la carte. Elle ne dépend que de la bibliothèque standard, de ``cryptography`` et,
en magasin TPM, de ``tpm2-pytss``.
"""

__version__ = "1.0.0"
