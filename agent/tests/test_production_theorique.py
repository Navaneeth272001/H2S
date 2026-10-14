"""Les deux formules de production ENR, vérifiées sur les exemples chiffrés de la spécification.

La spécification remise par le client le 04/10/2026 donne deux calculs complets, avec leur résultat.
Ce sont eux qui font foi : 264 W pour le solaire, ≈ 68,2 W pour l'éolien. Un test qui ne vérifierait
que « la fonction rend un nombre » ne garderait rien — c'est le NOMBRE qui est la spécification.
"""

import math

import pytest

from h2s_agent.energy_calculator import (
    RENDEMENT_EOLIEN_DEFAUT,
    RENDEMENT_PV_DEFAUT,
    RHO_AIR_KG_M3,
    SourceTheorique,
    borner_a_la_machine,
    production_eolienne_w,
    production_solaire_w,
    production_totale,
    surface_balayee_m2,
)


class TestExemplesDeLaSpecification:
    """Les deux calculs du document, repris chiffre pour chiffre."""

    def test_solaire_600_w_m2_sur_2_m2_a_22_pct_donne_264_w(self):
        # « P_PV = 600 × 2.0 × 0.22 = 264 W »
        assert production_solaire_w(600.0, 2.0, 0.22) == pytest.approx(264.0)

    def test_surface_balayee_d_un_rotor_de_1m80_vaut_2545_m2(self):
        # « A = π × 0,81 = 2,545 m² »
        assert surface_balayee_m2(1.8) == pytest.approx(2.545, abs=0.001)

    def test_eolien_5_m_s_sur_un_rotor_de_1m80_a_35_pct_donne_68_2_w(self):
        # « P_wind = 0,5 × 1,225 × 2,545 × 125 × 0,35 ≈ 68,2 W »
        assert production_eolienne_w(5.0, 1.8, 0.35) == pytest.approx(68.2, abs=0.1)

    def test_la_dependance_est_cubique_doubler_le_vent_multiplie_par_huit(self):
        # « Vent à 5 m/s → 68 W ; vent à 10 m/s → 544 W (8 fois plus !) »
        cinq = production_eolienne_w(5.0, 1.8, 0.35)
        dix = production_eolienne_w(10.0, 1.8, 0.35)
        assert dix == pytest.approx(cinq * 8, rel=1e-9)
        assert dix == pytest.approx(545.6, abs=1.0)

    def test_l_analyse_dimensionnelle_tient_le_resultat_est_bien_une_puissance(self):
        # kg/m³ × m² × m³/s³ = kg·m²/s³ = W. On vérifie la proportionnalité à chaque facteur, qui est
        # ce que l'analyse dimensionnelle garantit.
        base = production_eolienne_w(5.0, 1.8, 0.35, rho_kg_m3=RHO_AIR_KG_M3)
        assert production_eolienne_w(5.0, 1.8, 0.35, rho_kg_m3=2 * RHO_AIR_KG_M3) == pytest.approx(2 * base)
        assert production_eolienne_w(5.0, 1.8, 0.70) == pytest.approx(2 * base)
        # Surface ∝ D² : un rotor deux fois plus large balaie quatre fois plus.
        assert production_eolienne_w(5.0, 3.6, 0.35) == pytest.approx(4 * base)


class TestValeursParDefautDeLaSpecification:
    def test_les_rendements_par_defaut_sont_ceux_du_document(self):
        assert RENDEMENT_PV_DEFAUT == 0.22
        assert RENDEMENT_EOLIEN_DEFAUT == 0.35
        assert RHO_AIR_KG_M3 == 1.225

    def test_le_rendement_par_defaut_s_applique_quand_il_n_est_pas_donne(self):
        assert production_solaire_w(600.0, 2.0) == pytest.approx(264.0)
        assert production_eolienne_w(5.0, 1.8) == pytest.approx(68.2, abs=0.1)


class TestDonneesAbsentesOuAberrantes:
    """Une caractéristique manquante rend 0, jamais une valeur inventée.

    Un nombre inventé se propage jusqu'au modèle et devient une prévision ; un zéro se lit.
    """

    @pytest.mark.parametrize(
        "args",
        [
            (None, 2.0, 0.22),
            (600.0, None, 0.22),
            (600.0, 2.0, None),
            (0.0, 2.0, 0.22),
            (-100.0, 2.0, 0.22),
            (600.0, 0.0, 0.22),
            (600.0, 2.0, 0.0),
        ],
    )
    def test_solaire_sans_donnee_utilisable_rend_zero(self, args):
        assert production_solaire_w(*args) == 0.0

    @pytest.mark.parametrize(
        "args",
        [
            (None, 1.8, 0.35),
            (5.0, None, 0.35),
            (5.0, 1.8, None),
            (0.0, 1.8, 0.35),
            (-3.0, 1.8, 0.35),
            (5.0, 0.0, 0.35),
        ],
    )
    def test_eolien_sans_donnee_utilisable_rend_zero(self, args):
        assert production_eolienne_w(*args) == 0.0

    def test_un_diametre_absent_ne_donne_aucune_surface(self):
        assert surface_balayee_m2(None) == 0.0
        assert surface_balayee_m2(0.0) == 0.0
        assert surface_balayee_m2(-2.0) == 0.0


class TestBornesDeLaMachine:
    """La formule décrit le vent ; la machine, elle, a des limites.

    Sans ces bornes, le calcul annonce 4,4 kW à 20 m/s sur une turbine mise en drapeau à 18 — et c'est
    ce chiffre qui partirait au modèle de prédiction.
    """

    def test_sous_la_vitesse_de_demarrage_la_machine_ne_produit_rien(self):
        brute = production_eolienne_w(2.0, 1.8, 0.35)
        assert brute > 0
        assert borner_a_la_machine(brute, 2.0, demarrage_m_s=3.0, arret_m_s=25.0, puissance_nominale_w=None) == 0.0

    def test_au_dela_de_la_vitesse_d_arret_la_machine_est_en_securite(self):
        brute = production_eolienne_w(30.0, 1.8, 0.35)
        assert brute > 1000
        assert borner_a_la_machine(brute, 30.0, demarrage_m_s=3.0, arret_m_s=25.0, puissance_nominale_w=None) == 0.0

    def test_la_puissance_nominale_plafonne_le_resultat(self):
        brute = production_eolienne_w(15.0, 1.8, 0.35)
        assert brute > 1000
        borne = borner_a_la_machine(brute, 15.0, demarrage_m_s=3.0, arret_m_s=25.0, puissance_nominale_w=1000.0)
        assert borne == 1000.0

    def test_sans_aucune_borne_le_resultat_est_exactement_celui_de_la_specification(self):
        brute = production_eolienne_w(5.0, 1.8, 0.35)
        assert borner_a_la_machine(brute, 5.0, None, None, None) == pytest.approx(68.2, abs=0.1)


class TestProductionTotaleDuSite:
    def test_un_site_solaire_et_eolien_additionne_les_deux(self):
        sources = [
            SourceTheorique(type="solar", surface_m2=2.0, rendement_pv=0.22),
            SourceTheorique(type="wind", diametre_rotor_m=1.8, rendement_eolien=0.35),
        ]
        r = production_totale(sources, irradiance_w_m2=600.0, vitesse_vent_m_s=5.0)
        assert r["production_solaire_w"] == pytest.approx(264.0)
        assert r["production_eolienne_w"] == pytest.approx(68.2, abs=0.1)
        assert r["production_totale_w"] == pytest.approx(332.2, abs=0.1)

    def test_plusieurs_panneaux_s_additionnent(self):
        sources = [
            SourceTheorique(type="solar", surface_m2=2.0, rendement_pv=0.22),
            SourceTheorique(type="solar", surface_m2=3.0, rendement_pv=0.20),
        ]
        r = production_totale(sources, irradiance_w_m2=600.0, vitesse_vent_m_s=0.0)
        assert r["production_solaire_w"] == pytest.approx(264.0 + 360.0)

    def test_les_trois_nombres_sont_toujours_rendus_meme_a_zero(self):
        # Un champ absent du message obligerait le cloud à distinguer « pas calculé » de « zéro ».
        r = production_totale([], irradiance_w_m2=None, vitesse_vent_m_s=None)
        assert r == {
            "production_solaire_w": 0.0,
            "production_eolienne_w": 0.0,
            "production_totale_w": 0.0,
        }

    def test_une_source_sans_caracteristique_ne_contribue_pas(self):
        # Un panneau dont la surface n'est pas déclarée ne produit pas « un peu » : il ne produit rien
        # de calculable, et le dire est plus honnête que de supposer une surface.
        sources = [SourceTheorique(type="solar", rendement_pv=0.22)]
        r = production_totale(sources, irradiance_w_m2=600.0, vitesse_vent_m_s=5.0)
        assert r["production_totale_w"] == 0.0

    def test_un_type_inconnu_est_ignore_sans_lever(self):
        sources = [SourceTheorique(type="hydro", surface_m2=10.0)]
        r = production_totale(sources, irradiance_w_m2=600.0, vitesse_vent_m_s=5.0)
        assert r["production_totale_w"] == 0.0

    def test_la_turbine_en_drapeau_ne_promet_rien(self):
        sources = [
            SourceTheorique(
                type="wind",
                diametre_rotor_m=1.8,
                rendement_eolien=0.35,
                vent_demarrage_m_s=3.0,
                vent_arret_m_s=18.0,
            )
        ]
        r = production_totale(sources, irradiance_w_m2=0.0, vitesse_vent_m_s=20.0)
        assert r["production_eolienne_w"] == 0.0

    def test_la_formule_nue_reste_accessible_sans_bornes_declarees(self):
        sources = [SourceTheorique(type="wind", diametre_rotor_m=1.8, rendement_eolien=0.35)]
        r = production_totale(sources, irradiance_w_m2=0.0, vitesse_vent_m_s=20.0)
        attendu = 0.5 * RHO_AIR_KG_M3 * math.pi * 0.81 * 8000 * 0.35
        assert r["production_eolienne_w"] == pytest.approx(attendu, rel=1e-6)
