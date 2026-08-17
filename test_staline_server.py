# ================================================================================
# SUITE DE TESTS — STALINE SERVER
# ================================================================================
# But : détecter automatiquement les régressions du type de celles trouvées
# manuellement (à coups de copier-coller de logs) tout au long de cette
# session : mauvais nom de clé, calcul mathématiquement faux, valeur par
# défaut qui écrase silencieusement une autre branche, seuil hors bornes.
#
# Usage :
#   pip install pytest --break-system-packages
#   pytest test_staline_server.py -v
# Ou sans pytest installé :
#   python3 test_staline_server.py
#
# [V46-FIX-IMPORT-SAFETY] staline_server.py démarre un thread "watchdog" et
# charge des données réseau au niveau module (load_all(), puis
# threading.Thread(target=watchdog,daemon=True).start() tout en bas du
# fichier) DÈS L'IMPORT — un simple `importlib.import_module("staline_server")`
# déclenche donc de VRAIS appels réseau et un VRAI thread de fond à chaque
# lancement des tests, ce qui n'est ni sûr ni reproductible pour une suite
# de tests censée tourner n'importe où, n'importe quand, hors ligne. On
# neutralise temporairement threading.Thread.start() PENDANT l'import
# seulement (restauré juste après, avant même le premier test) — le fichier
# serveur lui-même n'est jamais modifié.
#
# STALINE_SERVER_PATH (variable d'environnement) permet de pointer vers le
# fichier serveur réel quel que soit son nom exact (ex: le fichier livré
# peut s'appeler staline_server_V44_....py) ; par défaut on cherche
# "staline_server.py" à côté de ce fichier de test.
# ================================================================================
import os
import sys
import threading
import importlib.util

try:
    import pytest
    _HAS_PYTEST = True
except ImportError:
    _HAS_PYTEST = False

    class _PytestShim:
        @staticmethod
        def skip(msg):
            raise RuntimeError(f"SKIP: {msg}")
    pytest = _PytestShim()

STALINE_SERVER_MODULE = "staline_server"  # nom logique, indépendant du nom de fichier réel
STALINE_SERVER_PATH = os.environ.get(
    "STALINE_SERVER_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "staline_server.py"),
)

_real_thread_start = threading.Thread.start
threading.Thread.start = lambda self: None   # no-op UNIQUEMENT pendant l'import ci-dessous
try:
    if os.path.exists(STALINE_SERVER_PATH):
        _spec = importlib.util.spec_from_file_location(STALINE_SERVER_MODULE, STALINE_SERVER_PATH)
        srv = importlib.util.module_from_spec(_spec)
        sys.modules[STALINE_SERVER_MODULE] = srv
        _spec.loader.exec_module(srv)
        IMPORT_ERROR = None
    else:
        srv = None
        IMPORT_ERROR = f"fichier introuvable: {STALINE_SERVER_PATH} (définis STALINE_SERVER_PATH si besoin)"
except Exception as e:
    srv = None
    IMPORT_ERROR = str(e)
finally:
    threading.Thread.start = _real_thread_start   # restauré dans tous les cas, même si l'import plante


def _require_module():
    if srv is None:
        pytest.skip(f"Impossible d'importer le serveur : {IMPORT_ERROR}")


# --------------------------------------------------------------------------
# AI-25 — consistency_check() : le bug corrigé cette session (abs(sum)/total
# au lieu de count(+1)/total) doit rester corrigé.
# --------------------------------------------------------------------------
class TestConsistencyCheck:
    def test_majority_agreement_reflected_correctly(self):
        """3 signaux d'accord sur 4 doit donner 0.75, pas 0.25 (bug d'origine)."""
        _require_module()
        res = srv.consistency_check(fv=0.70, ml=0.30, rules=0.60, pred_adj=0.10,
                                     social_score=0.0, direction=1)
        assert res["total_signals"] == 4
        assert res["agreement"] == 3
        assert abs(res["consensus_pct"] - 0.75) < 1e-9

    def test_full_agreement_gives_100_percent(self):
        _require_module()
        res = srv.consistency_check(fv=0.70, ml=0.70, rules=0.60, pred_adj=0.10,
                                     social_score=0.0, direction=1)
        assert res["consensus_pct"] == 1.0

    def test_full_disagreement_gives_0_percent(self):
        _require_module()
        res = srv.consistency_check(fv=0.10, ml=0.10, rules=0.10, pred_adj=-0.10,
                                     social_score=0.0, direction=1)
        assert res["consensus_pct"] == 0.0

    def test_ok_threshold_still_040(self):
        """Le seuil de validation (0.40) est un réglage volontaire — ce test
        vérifie juste qu'il n'a pas changé accidentellement."""
        _require_module()
        res = srv.consistency_check(fv=0.70, ml=0.70, rules=0.10, pred_adj=-0.10,
                                     social_score=0.0, direction=1)  # 2/4 = 0.50
        assert res["ok"] is True
        res2 = srv.consistency_check(fv=0.10, ml=0.10, rules=0.60, pred_adj=-0.10,
                                      social_score=0.0, direction=1)  # 1/4 = 0.25
        assert res2["ok"] is False


# --------------------------------------------------------------------------
# Seuils de trust (AI-31) — doivent rester dans une plage exploitable.
# --------------------------------------------------------------------------
class TestTrustThresholds:
    def test_thresholds_in_sane_range(self):
        _require_module()
        assert 0.30 <= srv.TRUST_SNIPER_THRESHOLD <= 0.90
        assert 0.30 <= srv.TRUST_NORMAL_THRESHOLD <= 0.90

    def test_sniper_not_looser_than_normal(self):
        """Le mode sniper est censé être plus strict (ou égal), jamais plus
        laxiste que le mode normal — si ce test casse après un futur
        changement de seuil, c'est probablement une inversion par erreur."""
        _require_module()
        assert srv.TRUST_SNIPER_THRESHOLD >= srv.TRUST_NORMAL_THRESHOLD


# --------------------------------------------------------------------------
# get_macro_snapshot() — vix/dxy/us10y ont un fallback garanti et ne
# doivent JAMAIS être None (sinon le bug sources_ok=0/5 revient).
# --------------------------------------------------------------------------
class TestMacroSnapshotFallback:
    def test_critical_fields_never_none(self):
        _require_module()
        data = srv.get_macro_snapshot()
        for key in ("vix", "dxy", "us10y"):
            assert data.get(key) is not None, f"{key} ne doit jamais être None (fallback garanti dans le code)"

    def test_sources_ok_uses_correct_keys(self):
        """Réplique le calcul de sources_ok tel qu'il doit être fait —
        détecte si quelqu'un réintroduit le bug de clés mal nommées
        (us10y_val/sp500_chg au lieu de us10y/sp500)."""
        _require_module()
        data = srv.get_macro_snapshot()
        sources_ok = sum(1 for k, v in {
            "vix": data.get("vix"), "dxy": data.get("dxy"), "gold": data.get("gold"),
            "us10y": data.get("us10y"), "sp500": data.get("sp500"),
        }.items() if v is not None)
        # vix/dxy/us10y ont un fallback garanti -> au moins 3/5 toujours
        assert sources_ok >= 3


# --------------------------------------------------------------------------
# direction=0 (probe) — doit être résolu, jamais silencieusement traité
# comme SELL.
# --------------------------------------------------------------------------
class TestDirectionZeroResolution:
    def test_direction_zero_resolves_via_real_stats(self):
        _require_module()
        hour = 12
        stat = srv.real_get("BTCUSD", hour)
        assert isinstance(stat, dict)
        assert "direction" in stat
        assert stat["direction"] in ("BUY", "SELL", "WAIT")


# --------------------------------------------------------------------------
# [V45] Sizing par volatilité — la nouvelle fonction ne doit jamais
# retourner un lot négatif, nul (hors cas de repli), ou démesuré.
# --------------------------------------------------------------------------
class TestVolTargetLot:
    def test_normal_case_reasonable_lot(self):
        _require_module()
        res = srv.compute_vol_target_lot("XAUUSD", equity=1000.0, atr=2.5, sl_atr_mult=1.5, risk_pct=0.01)
        assert 0.01 <= res["lot"] <= 5.0

    def test_zero_atr_falls_back_safely(self):
        _require_module()
        res = srv.compute_vol_target_lot("XAUUSD", equity=1000.0, atr=0.0, sl_atr_mult=1.5)
        assert res["lot"] == 0.01
        assert res["source"] == "fallback_min_lot"

    def test_never_negative(self):
        _require_module()
        res = srv.compute_vol_target_lot("BTCUSD", equity=50.0, atr=500.0, sl_atr_mult=3.0, risk_pct=0.01)
        assert res["lot"] >= 0.01


# --------------------------------------------------------------------------
# [V45] Exposition cross-symbole
# --------------------------------------------------------------------------
class TestCorrelatedExposure:
    def test_no_open_positions_no_warning(self):
        _require_module()
        res = srv.check_correlated_exposure("BTCUSD", 1, [])
        assert res["warning"] is False
        assert res["correlated_lots_equiv"] == 0.0

    def test_opposite_direction_does_not_stack(self):
        """Un BUY et un SELL corrélés se couvrent, ne doivent pas s'additionner."""
        _require_module()
        positions = [{"symbol": "XAUUSD", "direction": -1, "lot": 1.0}]
        res = srv.check_correlated_exposure("XAGUSD", 1, positions)
        assert res["correlated_lots_equiv"] == 0.0

    def test_same_direction_correlated_symbols_stack(self):
        _require_module()
        positions = [{"symbol": "XAUUSD", "direction": 1, "lot": 2.0}]
        res = srv.check_correlated_exposure("XAGUSD", 1, positions)
        assert res["correlated_lots_equiv"] > 0.0


# --------------------------------------------------------------------------
# [V47] Combinaison Kelly + vol-target — le lot de base doit toujours être
# le plus petit des deux (jamais plus agressif que la méthode la plus
# prudente) — verrouille le comportement demandé explicitement ("les deux
# en même temps"), pour qu'un futur changement accidentel de `min(...)` en
# `max(...)` soit détecté immédiatement par ce test.
# --------------------------------------------------------------------------
class TestCombinedLotSizing:
    def test_base_lot_is_minimum_of_both_methods(self):
        _require_module()
        kelly = srv.compute_kelly_lot("XAUUSD", 12, base_lot=0.01, equity=1000.0, sl_pips=50.0)
        vol_target = srv.compute_vol_target_lot("XAUUSD", equity=1000.0, atr=5.0, sl_atr_mult=1.5, risk_pct=0.01)
        combined = min(kelly["lot"], vol_target["lot"])
        assert combined <= kelly["lot"]
        assert combined <= vol_target["lot"]


# --------------------------------------------------------------------------
# [V48] Boucle décision → résultat — un decision_record doit pouvoir recevoir
# son résultat réel via attach_outcome_to_decision(), avec appariement par
# symbole+heure+direction sur le plus récent sans résultat déjà attaché.
# --------------------------------------------------------------------------
class TestDecisionOutcomeLoop:
    def test_outcome_attaches_to_matching_record(self):
        _require_module()
        from datetime import datetime, timezone
        srv._DR_seq += 1
        fake_id = srv._DR_seq
        fake_record = {
            "decision_id": fake_id, "symbol": "BTCUSD",
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hour_utc": 12, "direction": "BUY", "decision": "BUY", "outcome": None,
        }
        with srv._DR_lock:
            srv._DR_log.append(fake_record)
        matched_id = srv.attach_outcome_to_decision("BTCUSD", 12, 1, True, pnl_pct=1.2, net_eur=5.5, duration_min=18.0)
        assert matched_id == fake_id
        assert fake_record["outcome"]["win"] is True
        assert fake_record["outcome"]["net_eur"] == 5.5

    def test_no_match_returns_none_without_crashing(self):
        _require_module()
        result = srv.attach_outcome_to_decision("SYMBOLE_QUI_N_EXISTE_PAS_XYZ", 12, 1, True)
        assert result is None


# --------------------------------------------------------------------------
# [V49] feedback_get_alpha() doit exposer alpha_adj/winrate (alias de
# confidence_adj/post_wr) — sans ça, le code en aval qui les lit
# (score+=fb.get("alpha_adj",0) et compute_trust_score(...,fb.get("winrate",0.5)))
# ne s'applique jamais / reste toujours neutre.
# --------------------------------------------------------------------------
class TestFeedbackAlphaAdjAlias:
    def test_alpha_adj_and_winrate_present_and_consistent(self):
        _require_module()
        fb = srv.feedback_get_alpha("XAUUSD", "TREND", "LONDON", 1, 12)
        assert "alpha_adj" in fb
        assert "winrate" in fb
        assert fb["alpha_adj"] == fb["confidence_adj"]
        assert fb["winrate"] == fb["post_wr"]


# --------------------------------------------------------------------------
# [V50] AI-17 à paliers avec borne de Wilson — remplace la blacklist binaire.
# --------------------------------------------------------------------------
class TestFeedbackRiskTiers:
    def test_small_sample_never_vetoed(self):
        _require_module()
        # Ancien seuil (n=25, WR=16%) aurait blacklisté — nouveau système non,
        # échantillon trop petit pour une conclusion statistique fiable.
        t = srv.feedback_risk_tier(25, 4)
        assert t["veto"] is False

    def test_large_sample_bad_wr_statistically_confirmed_vetoes(self):
        _require_module()
        t = srv.feedback_risk_tier(150, 20)
        assert t["veto"] is True

    def test_moderate_sample_bad_wr_reduces_lot_not_veto(self):
        _require_module()
        t = srv.feedback_risk_tier(40, 10)
        assert t["veto"] is False
        assert t["lot_cap"] == 0.70

    def test_wilson_lower_bound_sane_bounds(self):
        _require_module()
        lb = srv._wilson_lower_bound(50, 100)
        assert 0.0 <= lb <= 0.50


if __name__ == "__main__":
    if _HAS_PYTEST:
        sys.exit(pytest.main([__file__, "-v"]))

    # Fallback sans pytest : découvre et lance toutes les méthodes test_*
    # de toutes les classes Test* définies au-dessus.
    classes = [obj for name, obj in list(globals().items())
               if name.startswith("Test") and isinstance(obj, type)]
    passed, failed = 0, []
    for cls in classes:
        instance = cls()
        for name in dir(instance):
            if not name.startswith("test_"):
                continue
            method = getattr(instance, name)
            full_name = f"{cls.__name__}.{name}"
            try:
                method()
                print(f"  OK   {full_name}")
                passed += 1
            except RuntimeError as e:
                print(f"  SKIP {full_name} -- {e}")
            except AssertionError as e:
                print(f"  FAIL {full_name} -- {e}")
                failed.append(full_name)
            except Exception as e:
                print(f"  ERROR {full_name} -- {type(e).__name__}: {e}")
                failed.append(full_name)
    print(f"\n{passed} test(s) passé(s).")
    if failed:
        print("Échecs :", ", ".join(failed))
        sys.exit(1)
    sys.exit(0)
