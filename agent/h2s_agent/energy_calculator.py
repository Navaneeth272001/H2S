"""Production ENR **théorique** calculée sur le boîtier, à partir de la météo (brique 18, §4.1.4).

Les deux formules sont celles de la spécification remise par le client le 04/10/2026. Elles sont
reproduites ici **telles quelles**, et les deux exemples chiffrés du document servent de tests :
264 W pour le solaire, 68,2 W pour l'éolien. Un écart sur l'un de ces deux nombres est un écart avec
la spécification, pas une approximation acceptable.

POURQUOI CE CALCUL EST SUR L'AGENT ET NON DANS LE CLOUD. C'est la demande explicite de la
spécification, et elle a trois raisons qui tiennent :

* **bande passante** — on envoie un résultat (trois nombres) au lieu des relevés météo bruts ;
* **charge du cloud** — le calcul ne remonte pas au moteur de prédiction ;
* **mode dégradé** — réseau coupé, le boîtier sait toujours estimer sa production théorique, et ses
  règles locales peuvent s'en servir.

CE QUE CE CALCUL EST, ET CE QU'IL N'EST PAS. C'est une production **théorique**, déduite de la météo et
des caractéristiques déclarées du matériel. Ce n'est **pas** une mesure : elle ne remplace jamais ce que
les compteurs remontent, et elle ne doit jamais être présentée comme de la production constatée. Les
deux voyagent séparément jusqu'au cloud, et c'est au modèle LightGBM de les confronter.

LIMITE PHYSIQUE ASSUMÉE. La formule éolienne est en V³ : une vitesse doublée donne huit fois la
puissance. Elle ne connaît donc ni la vitesse de démarrage, ni le palier nominal, ni l'arrêt de
sécurité d'une machine réelle — appliquée telle quelle, elle promettrait des kilowatts à 25 m/s sur une
turbine qui s'est mise en drapeau. La spécification ne demande pas ces bornes ; elles sont appliquées
**séparément** par ``borner_a_la_machine`` quand la courbe de la turbine est connue, et la formule nue
reste disponible pour qui veut exactement le chiffre du document.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Masse volumique de l'air au niveau de la mer (kg/m³), constante de la spécification.
RHO_AIR_KG_M3 = 1.225

#: Rendement photovoltaïque par défaut de la spécification (22 %), quand le panneau ne le déclare pas.
RENDEMENT_PV_DEFAUT = 0.22

#: Rendement global d'éolienne par défaut de la spécification (35 %).
RENDEMENT_EOLIEN_DEFAUT = 0.35


def surface_balayee_m2(diametre_m: float) -> float:
    """Surface balayée par le rotor : ``A = π × (D / 2)²``.

    Exemple de la spécification : D = 1,8 m → A = 2,545 m².
    """
    if diametre_m is None or diametre_m <= 0:
        return 0.0
    return math.pi * (diametre_m / 2.0) ** 2


def production_solaire_w(irradiance_w_m2: float, surface_m2: float, rendement: float = RENDEMENT_PV_DEFAUT) -> float:
    """Puissance photovoltaïque : ``P_PV = G × S × η_PV``.

    Exemple de la spécification : G = 600 W/m², S = 2,0 m², η = 0,22 → 264 W.

    Une valeur manquante ou négative rend 0,0 : un panneau dont on ne connaît pas la surface ne produit
    pas « un peu », il ne produit **rien de calculable** — et un zéro se lit, là où un nombre inventé
    se propage jusqu'au modèle.
    """
    if irradiance_w_m2 is None or surface_m2 is None or rendement is None:
        return 0.0
    if irradiance_w_m2 <= 0 or surface_m2 <= 0 or rendement <= 0:
        return 0.0
    return irradiance_w_m2 * surface_m2 * rendement


def production_eolienne_w(
    vitesse_vent_m_s: float,
    diametre_m: float,
    rendement: float = RENDEMENT_EOLIEN_DEFAUT,
    rho_kg_m3: float = RHO_AIR_KG_M3,
) -> float:
    """Puissance éolienne : ``P_wind = ½ × ρ × A × V³ × η``, avec ``A = π × (D / 2)²``.

    Exemple de la spécification : ρ = 1,225, D = 1,8 m, V = 5 m/s, η = 0,35 → ≈ 68,2 W.

    L'analyse dimensionnelle du document tient : kg/m³ × m² × m³/s³ = kg·m²/s³ = W.
    """
    if vitesse_vent_m_s is None or diametre_m is None or rendement is None:
        return 0.0
    if vitesse_vent_m_s <= 0 or diametre_m <= 0 or rendement <= 0:
        return 0.0
    surface = surface_balayee_m2(diametre_m)
    return 0.5 * rho_kg_m3 * surface * (vitesse_vent_m_s**3) * rendement


def borner_a_la_machine(
    puissance_w: float,
    vitesse_vent_m_s: float,
    demarrage_m_s: float | None,
    arret_m_s: float | None,
    puissance_nominale_w: float | None,
) -> float:
    """Applique à une puissance éolienne théorique les bornes de la machine, quand elles sont connues.

    La formule en V³ décrit la puissance **disponible dans le vent**, pas ce qu'une turbine donnée en
    tire. Une machine réelle ne démarre pas sous sa vitesse de démarrage, plafonne à sa puissance
    nominale, et se met en sécurité au-delà de sa vitesse d'arrêt. Sans ces bornes, le calcul annonce
    des kilowatts pendant qu'une turbine en drapeau ne produit rien — et c'est ce chiffre-là qui
    partirait au modèle.

    Chaque borne est **facultative** : absente, elle ne s'applique pas. Le résultat sans aucune borne
    est exactement celui de la spécification.
    """
    if puissance_w <= 0:
        return 0.0
    if demarrage_m_s is not None and vitesse_vent_m_s < demarrage_m_s:
        return 0.0
    if arret_m_s is not None and vitesse_vent_m_s >= arret_m_s:
        return 0.0
    if puissance_nominale_w is not None and puissance_nominale_w > 0:
        return min(puissance_w, puissance_nominale_w)
    return puissance_w


@dataclass(frozen=True)
class SourceTheorique:
    """Caractéristiques techniques déclarées d'une source de production du site.

    Elles viennent de la configuration de l'agent, qui les tient du cloud : ce sont les champs saisis
    dans « Paramètres → Production ». Rien n'est deviné ici — une caractéristique absente annule la
    contribution de la source, elle ne lui donne pas une valeur par défaut flatteuse.
    """

    type: str
    #: Solaire : surface totale des panneaux (m²).
    surface_m2: float | None = None
    #: Solaire : rendement photovoltaïque (0 à 1).
    rendement_pv: float | None = None
    #: Éolien : diamètre du rotor (m).
    diametre_rotor_m: float | None = None
    #: Éolien : rendement global de la machine (0 à 1).
    rendement_eolien: float | None = None
    #: Éolien, facultatif : bornes de la machine, pour ne pas promettre ce qu'elle ne donne pas.
    vent_demarrage_m_s: float | None = None
    vent_arret_m_s: float | None = None
    puissance_nominale_w: float | None = None


def production_totale(
    sources: list[SourceTheorique],
    irradiance_w_m2: float | None,
    vitesse_vent_m_s: float | None,
) -> dict[str, float]:
    """Production ENR théorique du site : somme des sources déclarées.

    Rend toujours les trois nombres, même à zéro : un champ absent du message obligerait le cloud à
    distinguer « pas calculé » de « calculé à zéro », et il ne le peut pas.
    """
    solaire = 0.0
    eolien = 0.0
    for s in sources:
        if s.type == "solar":
            solaire += production_solaire_w(
                irradiance_w_m2,
                s.surface_m2,
                s.rendement_pv if s.rendement_pv is not None else RENDEMENT_PV_DEFAUT,
            )
        elif s.type == "wind":
            brute = production_eolienne_w(
                vitesse_vent_m_s,
                s.diametre_rotor_m,
                s.rendement_eolien if s.rendement_eolien is not None else RENDEMENT_EOLIEN_DEFAUT,
            )
            eolien += borner_a_la_machine(
                brute,
                vitesse_vent_m_s or 0.0,
                s.vent_demarrage_m_s,
                s.vent_arret_m_s,
                s.puissance_nominale_w,
            )
    return {
        "production_solaire_w": round(solaire, 3),
        "production_eolienne_w": round(eolien, 3),
        "production_totale_w": round(solaire + eolien, 3),
    }
