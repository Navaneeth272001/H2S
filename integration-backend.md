# IOTA Service — Backend Integration

Ce document explique comment un Backend (Java, ou tout autre langage) doit
appeler `iota-service` en REST/JSON. Vous n'avez besoin de rien connaître
à TypeScript, Node.js, au SDK `@iota/iota-sdk`, ni à la cryptographie
(keypair/clé privée) — ce sont des détails internes que ce document ne
mentionne jamais dans le contrat.

⚠️ **État réel au moment de la rédaction** : ce service est **prêt à être
intégré via REST/JSON**. Il n'est **pas encore réellement intégré** à un
Backend Java (le Backend Java n'existe pas encore dans cet environnement).
Aucune transaction avec transfert de valeur réel n'a été démontrée (les
wallets Devnet utilisés en développement ne sont pas financés) — le
transfert de coin IOTA natif sur Devnet est implémenté et testé côté
service, pas validé avec de vrais fonds.

## 1. Architecture

```
Backend Java
    │
    │ REST / JSON
    ▼
IOTA Service
    │
    ├──── PostgreSQL   (transactions, idempotence)
    │
    └──── IOTA Rebased (réseau Devnet aujourd'hui)
```

Principe : **le Backend décide, le service IOTA exécute et fournit les
références** (adresse, digest, statut). Le service ne prend aucune
décision métier — il ne connaît ni les prix, ni le matching, ni la
tokenisation, ni aucune règle H2S.

## 2. Base URL

L'URL dépend entièrement de l'environnement de déploiement — **non
définie par ce service lui-même**.

Exemple local (développement) :
```
http://localhost:4100
```
⚠️ Ceci n'est **pas** une URL de production. L'URL réelle de déploiement
est **[À VALIDER avec DevOps]**.

## 3. Endpoints — vue d'ensemble

| Méthode | URL | Rôle |
|---|---|---|
| GET | `/health` | Contrôle de santé |
| GET | `/api/iota/network` | Réseau IOTA configuré |
| GET | `/api/iota/wallets` | Liste des wallets |
| POST | `/api/iota/wallets` | Création d'un wallet |
| GET | `/api/iota/wallets/{walletId}` | Détail d'un wallet |
| GET | `/api/iota/wallets/{walletId}/balance` | Balance réelle (Devnet) |
| POST | `/api/iota/transactions` | Envoi d'une transaction |
| GET | `/api/iota/transactions/{digest}` | Consultation d'une transaction |
| POST | `/api/iota/reconcile` | Réconciliation PostgreSQL ↔ IOTA (lecture seule) |

Toutes les requêtes et réponses sont en JSON (`Content-Type: application/json`).
Aucun header spécifique requis aujourd'hui au-delà de `Content-Type` sur
les `POST` — voir Section 13 (Sécurité) pour l'authentification, absente.

## 4. Wallets

### POST /api/iota/wallets

Request :

| Champ | Type | Obligatoire | Validation |
|---|---|---|---|
| `walletId` | string | oui | 1-64 caractères, `A-Za-z0-9_-` uniquement |

```bash
curl -X POST http://localhost:4100/api/iota/wallets \
  -H "Content-Type: application/json" \
  -d '{"walletId":"WALLET-PROD-001"}'
```

Response (`201 Created`) :
```json
{ "walletId": "WALLET-PROD-001", "address": "0x...", "network": "devnet", "createdAt": "2026-09-22T08:16:28.572Z" }
```

Erreurs : `400 INVALID_REQUEST_BODY`, `400 INVALID_WALLET_ID`, `409 WALLET_ALREADY_EXISTS`.

### GET /api/iota/wallets/{walletId}

Response (`200`) : même forme que ci-dessus. Erreurs : `400 INVALID_WALLET_ID`, `404 WALLET_NOT_FOUND`, `500 WALLET_DATA_ERROR`.

### GET /api/iota/wallets

Response (`200`) : `{ "wallets": [ {...}, {...} ] }`.

**Important pour vous, développeur Java : aucune de ces réponses ne
contient jamais de clé privée, quelle que soit la requête.** C'est
garanti par construction côté service (testé explicitement) — vous n'avez
jamais besoin de manipuler de matériel cryptographique, seulement des
identifiants (`walletId`) et des adresses publiques.

## 5. Balance

### GET /api/iota/wallets/{walletId}/balance

Appel réseau réel vers Devnet à chaque requête (pas de cache).

```json
{
  "walletId": "WALLET-PROD-001",
  "address": "0x...",
  "network": "devnet",
  "balance": { "coinType": "0x2::iota::IOTA", "coinObjectCount": 0, "totalBalance": "0" }
}
```

`totalBalance` est en **nanos** (chaîne de caractères — traitez-la comme
un `BigInteger`/`long` côté Java, jamais un `double`). 1 IOTA = 10⁹ nanos.
Une balance à `0` est un résultat **normal**, pas une erreur.

Erreurs : `404 WALLET_NOT_FOUND`, `502 IOTA_NETWORK_ERROR`.

## 6. Transactions

### POST /api/iota/transactions

Transfert de coin IOTA natif entre deux adresses (POC actuel — pas de
tokenisation, pas de logique métier H2S).

Request :

| Champ | Type | Obligatoire | Validation |
|---|---|---|---|
| `requestId` | string | oui | 1-128 caractères, `A-Za-z0-9_-` — voir Section 7 |
| `fromWalletId` | string | oui | wallet existant |
| `toAddress` | string | oui | `0x` + hexadécimal |
| `amount` | string | oui | entier positif, **en nanos** — traitez comme `BigInteger`, jamais `double`/`float` |

```bash
curl -X POST http://localhost:4100/api/iota/transactions \
  -H "Content-Type: application/json" \
  -d '{"requestId":"req-devnet-001","fromWalletId":"WALLET-A","toAddress":"0x...","amount":"1000"}'
```

Response — le code HTTP **dépend du résultat réel**, pas seulement du fait
que la requête a été acceptée :

| `status` | Code HTTP | Signification |
|---|---|---|
| `confirmed` | 201 | Confirmée on-chain, `digest` présent |
| `failed` | 200 | Traitée correctement, mais échec métier/blockchain (ex. fonds insuffisants) — **ce n'est pas une erreur HTTP** |
| `pending` | 202 | Une exécution pour ce `requestId` est déjà en cours ailleurs |

```json
{
  "requestId": "req-devnet-001",
  "status": "failed",
  "fromWalletId": "WALLET-A",
  "fromAddress": "0x...",
  "toAddress": "0x...",
  "amount": "1000",
  "network": "devnet",
  "errorCode": "IOTA_SDK_ERROR",
  "error": "No valid gas coins found for the transaction.",
  "createdAt": "...",
  "updatedAt": "..."
}
```

Erreurs (avant toute tentative) : `400 INVALID_REQUEST_BODY/INVALID_REQUEST_ID/INVALID_WALLET_ID/INVALID_TO_ADDRESS/INVALID_AMOUNT`, `404 WALLET_NOT_FOUND`, `403 NETWORK_NOT_ALLOWED`.

### GET /api/iota/transactions/{digest}

Interroge le réseau IOTA directement (pas la base du service).
```json
{ "digest": "...", "status": "confirmed", "timestampMs": "..." }
```
Erreurs : `404 TRANSACTION_NOT_FOUND`, `502 IOTA_NETWORK_ERROR`.

## 7. RequestId

`requestId` identifie **une opération logique unique**, choisie et générée
par le Backend (pas par ce service). **Obligatoire pour `POST
/api/iota/transactions`.**

Règle à respecter absolument côté Backend Java : **un `requestId` = une
seule tentative logique, y compris en cas de retry** (voir Section 12).
Ne générez jamais un nouveau `requestId` pour rejouer la même opération.

## 8. Idempotence

Envoyer deux fois le même `requestId` **n'exécute jamais deux fois**
l'opération blockchain — le résultat existant (quel que soit son statut)
est retourné tel quel.

**Persistante** : garantie par PostgreSQL (contrainte `UNIQUE` sur
`request_id`, opération atomique `INSERT ... ON CONFLICT DO NOTHING`), pas
par la mémoire du processus — testé contre un vrai redémarrage du service
et une vraie concurrence.

## 9. Statuts

- **`pending`** : enregistré, résultat final pas encore connu.
- **`confirmed`** : IOTA a réellement confirmé le succès.
- **`failed`** : IOTA a signalé un échec, ou l'exécution a échoué avant
  diffusion (fonds insuffisants, adresse invalide...).

## 10. Reconciliation

### POST /api/iota/reconcile

**Lecture seule — ne modifie jamais PostgreSQL.** Compare l'état persisté
à l'état réel observé sur IOTA, pour **une** transaction (par `requestId`).

```bash
curl -X POST http://localhost:4100/api/iota/reconcile \
  -H "Content-Type: application/json" \
  -d '{"requestId":"req-devnet-001"}'
```

```json
{
  "requestId": "req-devnet-001",
  "postgresStatus": "confirmed",
  "postgresDigest": "...",
  "iotaStatus": "success",
  "outcome": "coherent",
  "checkedAt": "..."
}
```

`outcome` possibles : `coherent`, `inconsistent`, `not_found` (digest
inconnu sur IOTA), `unable_to_check` (impossible de vérifier), `no_digest`
(l'enregistrement PostgreSQL n'a pas encore de digest — cas normal pour
`pending` ou un échec survenu avant diffusion).

⚠️ **Limitation connue** : un enregistrement `pending` n'a jamais de
digest dans le modèle actuel — si le service plantait entre la diffusion
réelle d'une transaction et l'enregistrement de son résultat, cette
transaction resterait `pending` **sans moyen de la réconcilier**
automatiquement avec cette version du service. Pas de réconciliation par
lot (toute la table) dans cette version — un `requestId` à la fois.

Erreurs : `400`, `404 REQUEST_ID_NOT_FOUND`.

## 11. Timeouts

**[À VALIDER]** — aucune valeur de timeout n'est imposée par ce document,
elle dépend du déploiement réel. Point à savoir : `POST
/api/iota/transactions` attend la résolution complète de la transaction
sur Devnet avant de répondre (pas d'exécution asynchrone en tâche de fond)
— prévoir un timeout HTTP côté Backend Java suffisamment généreux (l'ordre
de grandeur observé en développement est de 1 à 15 secondes selon l'état
du réseau Devnet, **pas une garantie contractuelle**).

## 12. Retry

**Règle impérative** : en cas de timeout ou d'échec réseau côté Backend
pendant un appel à `POST /api/iota/transactions`, le retry doit réutiliser
**exactement le même `requestId`**. Le service retrouvera l'opération déjà
enregistrée (ou en cours) plutôt que d'en déclencher une deuxième. Générer
un nouveau `requestId` pour un retry casserait la garantie d'idempotence.

## 13. Sécurité

- **Authentification Backend ↔ IOTA Service : [À VALIDER — non
  implémentée].** Ce service ne doit pas être exposé publiquement en
  l'état — aucun mécanisme d'authentification/autorisation n'existe
  aujourd'hui. Emplacement prévu pour un futur middleware :
  `handleRequest()` dans `src/server.ts`, avant le routage.
- Aucune clé privée, seed ou mnemonic n'est jamais retournée par l'API,
  quelle que soit la route — vérifié par tests automatisés.
- Toutes les erreurs suivent un format uniforme `{"error":{"code","message"}}`,
  sans stack trace ni détail interne.

## 14. Devnet / Testnet / Mainnet

Aujourd'hui : **Devnet uniquement**, appliqué par un garde-fou explicite
dans le code (refus de démarrage si `mainnet` hors production, refus des
transactions si le réseau configuré n'est pas `devnet`). La progression
vers Testnet puis Mainnet n'est **pas** une validation de mise en
production — elle nécessitera une décision d'équipe explicite avant toute
modification de ce garde-fou.

## 15. Limitations actuelles

- Transfert de valeur réel **non démontré** (aucun wallet financé dans cet
  environnement de développement).
- Aucune authentification.
- Idempotence testée en instance unique + simulation de redémarrage ; **pas
  testée avec deux processus `iota-service` réellement distincts**.
- Réconciliation : une transaction à la fois, lecture seule, ne corrige
  rien automatiquement.
- Aucune tokenisation, aucun asset métier, aucun certificat, aucun
  contrat Move, aucun settlement — hors périmètre de ce service à ce jour.
- PostgreSQL : base dédiée en développement local ; topologie de
  production (partagée avec le Backend ou dédiée) **[À VALIDER]**.

## 16. Exemples curl

```bash
# Santé
curl http://localhost:4100/health

# Créer un wallet
curl -X POST http://localhost:4100/api/iota/wallets -H "Content-Type: application/json" -d '{"walletId":"WALLET-A"}'

# Balance
curl http://localhost:4100/api/iota/wallets/WALLET-A/balance

# Transaction
curl -X POST http://localhost:4100/api/iota/transactions -H "Content-Type: application/json" \
  -d '{"requestId":"req-001","fromWalletId":"WALLET-A","toAddress":"0x...","amount":"1000"}'

# Consultation
curl http://localhost:4100/api/iota/transactions/<digest>

# Réconciliation
curl -X POST http://localhost:4100/api/iota/reconcile -H "Content-Type: application/json" -d '{"requestId":"req-001"}'
```

---

## Informations nécessaires lors de l'intégration Backend Java

À rassembler/valider avant de connecter un vrai Backend Java à ce service :

- **URL du service IOTA** en environnement réel — [À VALIDER avec DevOps]
- **Port** réel de déploiement — [À VALIDER]
- **Authentification** Backend ↔ Service — [À VALIDER], non implémentée
- **`requestId`** : qui le génère côté Java, sous quel format exact — [À VALIDER avec l'équipe Backend]
- **Timeout** HTTP côté client Java — [À VALIDER selon déploiement]
- **Stratégie de retry** côté Java — doit réutiliser le même `requestId` (règle imposée, voir Section 12)
- **Format JSON** : déjà figé et testé (voir Sections 4-10)
- **Gestion des erreurs** côté Java : format uniforme `{"error":{"code","message"}}`, déjà figé et testé
- **Environnement réseau IOTA cible** (Devnet aujourd'hui, Testnet/Mainnet plus tard) — [À VALIDER, décision d'équipe requise]
- **Configuration PostgreSQL** de production (partagée ou dédiée) — [À VALIDER]
