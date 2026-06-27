"""
SMART_HOUR_ENGINE.py
====================
Moteur de décision horaire intelligent — branché dans build_decision() V30.

Philosophie: PAS de dead zones. PAS de blocages horaires fixes.
L'objectif est MAX GAIN, MIN PERTE.

Logique:
  1. Lire la direction dominante réelle par heure (stats_10y.json + trades réels)
  2. Croiser avec la macro temps réel (DXY, VIX, xau_bias)
  3. Si direction demandée = direction perdante historique → FORCER la bonne
  4. Si macro confirme → booster confiance + lot
  5. Si macro contredit fortement (score < -0.4) → réduire lot (jamais bloquer)
  6. Jamais de veto pur sauf news blackout (géré ailleurs)

Interface attendue par staline_server.py:
  from SMART_HOUR_ENGINE import smart_hour_decision, get_forced_direction, get_summary

  she = smart_hour_decision(symbol, hour_utc, direction, macro, hist_bias)
  she retourne dict avec:
    priority          : "HIGH"/"MEDIUM"/"LOW"/"NONE"
    can_trade         : bool
    veto              : str ou None
    direction_changed : bool
    force_direction   : "BUY"/"SELL"
    final_direction   : 1/-1
    confidence        : float 0-1
    lot_factor        : float (1.0 = normal, 1.2 = boost, 0.7 = réduit)
    score_adj         : float (-0.1 à +0.1)
    bad_dir_penalty   : float
    sources_aligned   : int (0-3)
    macro_score       : float
    hist_score        : float
    macro_note        : str
    reason            : str
    rule              : dict
"""

import json, os, logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("SMART_HOUR_ENGINE")

# ──────────────────────────────────────────────────────────────────────────────
# DONNÉES RÉELLES — Direction dominante par heure par symbole
# Source: 33 012 trades réels Jan-Avr 2026 + 9 351 trades antérieurs
# Format: {heure: (dir_dominante, avantage_$, buy_avg, sell_avg, n_trades)}
# dir_dominante: "BUY", "SELL", "DUAL_LOSE" (les 2 perdent), "WEAK"
# ──────────────────────────────────────────────────────────────────────────────

REAL_HOUR_DIRECTION = {
    "XAUUSD": {
        # H00-H05: SELL légèrement dominant selon stats réelles
        0:  ("SELL", 22,  42,  65,  56),
        1:  ("SELL", 22,  35,  57, 108),
        2:  ("BUY",  61,  61,   0,   4),
        3:  ("SELL", 73,  73, 147,  13),
        4:  ("SELL", 55,  59, 115,  23),
        5:  ("SELL", 37,  44,  81,  50),
        7:  ("BUY",  40,  40,   0,   8),
        8:  ("BUY",  24, 103,  80,  20),
        9:  ("SELL", 14,  69,  83,  17),
        10: ("SELL", 31,  61,  92,  93),
        11: ("SELL", 49,  57, 106, 117),
        12: ("SELL", 41,  70, 111, 109),
        13: ("SELL", 20,  51,  71, 172),
        14: ("BUY",  28, 110,  82, 654),
        15: ("BUY",  15, 120, 105, 571),
        16: ("SELL",  7, 108, 115, 170),
        17: ("BUY",  29, 121,  91,  23),
        18: ("BUY",  46, 115,  69, 361),
        19: ("BUY",  89, 192, 104, 323),
        20: ("BUY", 154, 321, 167, 680),
        22: ("BUY",  41, 198, 157, 106),
    },
    "XAGUSD": {
        0:  ("BUY", 113, 129,  15,  14),
        1:  ("BUY",  17, 330, 313,  83),
        2:  ("SELL", 31, 167, 197,  76),
        3:  ("BUY",   7, 182, 175,  47),
        5:  ("SELL", 31,  97, 128,  91),
        7:  ("BUY",   9, 150, 141,  30),
        8:  ("SELL", 41,  43,  84,  24),
        9:  ("BUY", 131, 131,   0,   6),
        10: ("BUY",  66, 214, 148,  38),
        11: ("BUY", 104, 235, 131,  28),
        12: ("BUY",  16,  70,  53,  64),
        13: ("BUY",   9, 175, 166, 229),
        14: ("SELL", 43, 190, 233, 141),
        15: ("BUY",  12, 227, 214, 121),
        16: ("SELL", 51, 205, 255,  93),
        17: ("SELL", 32, 126, 157,  65),
        18: ("BUY", 132, 358, 226,  15),
        19: ("SELL", 90, 176, 267,  32),
        20: ("SELL", 64, 221, 284,  30),
        22: ("SELL", 66,  85, 151,  68),
        23: ("SELL", 44, 129, 173,  21),
    },
    "EURUSD": {
        0:  ("SELL",  9,  60,  69, 244),
        1:  ("SELL",  4,  65,  70, 124),
        2:  ("BUY",  69, 127,  59, 105),
        3:  ("BUY",  34,  50,  16,  21),
        4:  ("SELL", 19,  53,  73,  33),
        6:  ("SELL", 178,  0, 178,  33),
        9:  ("BUY",  18,  63,  45,  74),
        12: ("BUY",  94, 200, 106, 307),
        13: ("BUY", 112, 225, 113, 292),
        14: ("BUY",  67, 173, 107, 159),
        15: ("BUY", 111, 225, 114, 586),
        16: ("BUY",  35,  92,  57, 112),
        17: ("BUY",  62, 123,  61, 165),
        18: ("SELL",  8,  14,  22,  20),
        22: ("BUY",  18,  65,  47, 100),
        23: ("SELL", 34,  57,  90, 432),
    },
    "USDJPY": {
        0:  ("SELL", 11,  50,  61, 605),
        1:  ("BUY",   7,  64,  58, 326),
        4:  ("SELL",  1,  59,  60,  64),
        6:  ("SELL", 36,  36,  72,  24),
        7:  ("BUY",  35,  85,  50, 466),
        8:  ("SELL", 13,  58,  71, 208),
        9:  ("SELL", 34,  52,  85, 544),
        10: ("SELL", 38,  47,  86,  59),
        11: ("BUY",   9,  43,  34,  70),
        12: ("SELL", 48,  56, 104, 835),
        13: ("SELL", 63,  70, 133, 162),
        14: ("SELL",  1,  94,  95, 364),
        16: ("BUY",   9,  40,  31,  48),
        17: ("SELL", 18,  56,  74, 147),
        18: ("BUY",  20,  53,  33,  29),
        19: ("BUY",  10,  43,  33,  93),
        23: ("SELL",  5,  47,  52,  80),
    },
    "BTCUSD": {
        0:  ("BUY",  38, 130,  92,  35),
        1:  ("BUY", 203, 413, 210,  50),
        2:  ("SELL", 13,  35,  48, 214),
        3:  ("SELL",  4,  44,  48,  85),
        5:  ("SELL",  6,  59,  65, 132),
        6:  ("BUY",  27, 100,  73,  65),
        7:  ("BUY",   6,  54,  48, 157),
        9:  ("SELL",  5,  44,  50, 105),
        10: ("BUY",  23, 101,  78,  35),
        11: ("BUY",  23,  82,  59,  31),
        13: ("BUY",  11,  85,  75, 250),
        14: ("SELL", 12,  78,  90, 236),
        15: ("SELL",  5,  66,  70, 301),
        16: ("BUY",   6,  78,  73, 241),
        17: ("BUY",   8,  89,  81, 179),
        18: ("SELL",  6,  63,  69, 189),
        19: ("SELL", 11,  77,  88, 202),
        20: ("SELL", 14,  65,  78, 143),
        21: ("SELL", 23,  84, 107, 241),
        22: ("SELL", 20,  64,  84,  85),
        23: ("SELL", 37,  37,  73,  29),
    },
    "AUDUSD": {
        4:  ("BUY",   6,  62,  56, 281),
        15: ("BUY",  25,  77,  52, 1298),
        16: ("BUY",  33,  95,  62,  42),
        17: ("BUY",  63, 125,  61, 144),
        19: ("SELL",  1,  12,  13, 465),
        20: ("DUAL_LOSE", 0, -42, -38, 270),  # Les 2 directions perdent
    },
    "GBPUSD": {
        14: ("BUY",  74,  74,   0,  69),
        17: ("BUY",  38,  38,   0,  73),
        18: ("BUY",  31,  31,   0,  80),
        19: ("BUY",  33,  33,   0, 111),
    },
    "USDCHF": {
        17: ("BUY",  42,  42,   0,  61),
        18: ("BUY",  55,  55,   0,  74),
        20: ("BUY",  48,  48,   0,  78),
    },
    "GBPJPY": {
        16: ("BUY",  96,  96,   0, 195),
        17: ("BUY", 159, 159,   0,  51),
        18: ("BUY", 121, 121,   0,  10),
    },
    "NZDUSD": {
        17: ("BUY",  37,  37,   0,  52),
        19: ("BUY",  28,  28,   0,  54),
    },
}

# Heures de transition (changement de direction imminent = prudence lot)
TRANSITION_HOURS = {
    "XAUUSD": {13, 17},   # H13→H14 SELL→BUY, H17→H18 SELL→BUY
    "XAGUSD": {9, 17, 18},
    "EURUSD": {1, 11, 22},
    "USDJPY": {6, 11, 19},
    "BTCUSD": {1, 9, 13},
}

def _normalize(sym: str) -> str:
    return sym.upper().replace("M","").strip()


# ──────────────────────────────────────────────────────────────────────────────
# STATS 10 ANS — chargées depuis stats_10y.json
# ──────────────────────────────────────────────────────────────────────────────

_stats10y: dict = {}

def _load_stats10y():
    global _stats10y
    for path in ["stats_10y.json", "/opt/render/project/src/stats_10y.json"]:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                _stats10y = data.get("symbols", {})
                logger.info("[SHE] stats_10y.json chargé: %d symboles", len(_stats10y))
                return
            except Exception as e:
                logger.warning("[SHE] Erreur lecture stats_10y.json: %s", e)
    logger.warning("[SHE] stats_10y.json absent — stats 10 ans non disponibles")

_load_stats10y()


def _get_10y_direction(sym: str, hour: int) -> Optional[str]:
    """Direction 10 ans depuis stats_10y.json pour ce symbole/heure."""
    s = _stats10y.get(sym, {})
    hs = s.get("hour_stats", {}).get(str(hour), {})
    if hs and not hs.get("insufficient") and hs.get("n_samples", 0) >= 50:
        return hs.get("direction")  # "BUY"/"SELL"/"NEUTRAL"
    return None


def _get_10y_bull_rate(sym: str, hour: int) -> float:
    s = _stats10y.get(sym, {})
    hs = s.get("hour_stats", {}).get(str(hour), {})
    return hs.get("bull_rate", 0.5)


# ──────────────────────────────────────────────────────────────────────────────
# SCORE MACRO — interprète DXY, VIX, xau_bias, fear_greed
# Retourne float: +1.0 = très bullish, -1.0 = très bearish, 0 = neutre
# ──────────────────────────────────────────────────────────────────────────────

def _macro_score_for_sym(sym: str, macro: Optional[dict]) -> tuple:
    """
    Calcule le score macro pour un actif donné.
    Retourne (score_float, note_str)
    score > 0 = favorable à BUY
    score < 0 = favorable à SELL
    """
    if not macro:
        return 0.0, "macro_absent"

    dxy       = float(macro.get("dxy", 100.0) or 100.0)
    vix       = float(macro.get("vix", 20.0)  or 20.0)
    xau_bias  = float(macro.get("xau_bias", 0.0) or 0.0)
    sp500_ret = float(macro.get("sp500_return", 0.0) or 0.0)
    us10y     = float(macro.get("us10y", 4.3) or 4.3)
    fg        = int(macro.get("fear_greed", 50) or 50)

    # DXY signal (normalisé autour de 100)
    dxy_signal  = (100.0 - dxy) / 15.0       # DXY 85 → +1.0, DXY 115 → -1.0
    dxy_signal  = max(-1.0, min(1.0, dxy_signal))

    # VIX signal
    if vix < 15:   vix_signal = 0.3   # Calme → risk on → BUY actifs risqués
    elif vix < 20: vix_signal = 0.0
    elif vix < 28: vix_signal = -0.2  # Stress
    else:          vix_signal = -0.6  # Crise → risk off

    # Fear & Greed
    fg_signal = (fg - 50.0) / 50.0           # 0=Peur extrême=-1, 100=Avidité=+1

    # US10Y signal (taux hauts = bearish actifs sans rendement)
    us10y_signal = -(us10y - 4.0) / 3.0      # 4%=0, 7%=-1.0, 1%=+1.0
    us10y_signal = max(-1.0, min(1.0, us10y_signal))

    # SP500 signal
    sp500_signal = max(-0.5, min(0.5, sp500_ret / 2.0))

    # Calcul par actif (corrélations institutionnelles connues)
    if "XAU" in sym:
        # Or: corrélé négativement au DXY, positivement au VIX et inflation
        score = (-dxy_signal * 0.45 +    # DXY fort → XAU baisse
                 -vix_signal * 0.30 +    # VIX haut → XAU monte (risque off)
                 xau_bias   * 0.15 +     # Biais XAU direct depuis serveur
                 -us10y_signal * 0.10)   # Taux hauts → XAU sous pression
        note = f"DXY={dxy:.1f}({-dxy_signal*0.45:+.2f}) VIX={vix:.1f}({-vix_signal*0.30:+.2f})"

    elif "XAG" in sym:
        score = (-dxy_signal * 0.40 +
                 -vix_signal * 0.25 +
                 sp500_signal * 0.20 +   # Argent = métal industriel aussi
                 xau_bias    * 0.15)
        note = f"DXY={dxy:.1f} VIX={vix:.1f} SP500={sp500_ret:+.1f}%"

    elif "BTC" in sym or "ETH" in sym:
        # Crypto: risk on = BUY, VIX haut = SELL
        score = (vix_signal * 0.40 +     # VIX haut → BTC baisse
                 fg_signal  * 0.30 +     # Peur → SELL, Avidité → BUY
                 sp500_signal * 0.20 +
                 dxy_signal  * 0.10)     # DXY fort → BTC baisse
        note = f"VIX={vix:.1f} FG={fg} SP500={sp500_ret:+.1f}%"

    elif "JPY" in sym:
        if sym.startswith("USD"):
            # USDJPY: DXY fort → BUY, VIX haut → SELL (JPY safe haven)
            score = (dxy_signal  * 0.50 +
                     -vix_signal * 0.30 +   # VIX haut → JPY s'apprécie → USDJPY baisse
                     -fg_signal  * 0.20)
        else:  # GBPJPY etc
            score = (-vix_signal * 0.50 +   # VIX haut → JPY fort → GBPJPY baisse
                     sp500_signal * 0.30 +
                     fg_signal    * 0.20)
        note = f"DXY={dxy:.1f} VIX={vix:.1f}"

    elif sym.startswith("EUR"):
        # EURUSD: DXY faible → EUR monte
        score = (-dxy_signal  * 0.55 +
                 sp500_signal * 0.20 +
                 fg_signal    * 0.15 +
                 -us10y_signal * 0.10)
        note = f"DXY={dxy:.1f}({-dxy_signal*0.55:+.2f})"

    elif sym.startswith("AUD") or sym.startswith("NZD"):
        # Aussie/Kiwi: risk on currencies
        score = (sp500_signal * 0.40 +
                 fg_signal    * 0.30 +
                 -dxy_signal  * 0.30)
        note = f"SP500={sp500_ret:+.1f}% FG={fg}"

    elif sym.startswith("GBP") or sym.startswith("CHF") or sym.startswith("USD"):
        score = (-dxy_signal * 0.50 +
                 sp500_signal * 0.30 +
                 fg_signal    * 0.20)
        note = f"DXY={dxy:.1f}"

    else:
        score = -dxy_signal * 0.4 + sp500_signal * 0.3 + fg_signal * 0.3
        note = f"generic macro"

    return round(max(-1.0, min(1.0, score)), 4), note


# ──────────────────────────────────────────────────────────────────────────────
# FONCTIONS PUBLIQUES ATTENDUES PAR LE SERVEUR
# ──────────────────────────────────────────────────────────────────────────────

def smart_hour_decision(
    symbol:    str,
    hour_utc:  int,
    direction: int,           # 1=BUY demandé, -1=SELL demandé
    macro:     Optional[dict] = None,
    hist_bias: Optional[dict] = None,
) -> dict:
    """
    Décision principale SMART_HOUR.
    Appelée par _smart_hour_gate() dans build_decision.

    Principe:
    - Aucun blocage horaire fixe (pas de dead zones)
    - Si direction perdante → forcer la gagnante
    - Si macro contredit faiblement → réduire lot
    - Si macro contredit fortement ET stats aussi → réduire lot 50% (jamais bloquer)
    - Si tout est aligné → booster lot et confiance
    """
    sym  = _normalize(symbol)
    want_buy = (direction == 1)

    # Résultat par défaut: NONE (pas d'interférence)
    base = {
        "priority":         "NONE",
        "can_trade":        True,
        "veto":             None,
        "direction_changed": False,
        "force_direction":  "BUY" if want_buy else "SELL",
        "final_direction":  direction,
        "confidence":       0.60,
        "lot_factor":       1.0,
        "score_adj":        0.0,
        "bad_dir_penalty":  0.0,
        "sources_aligned":  0,
        "macro_score":      0.0,
        "hist_score":       0.5,
        "macro_note":       "",
        "reason":           "Aucune règle spécifique",
        "rule":             {},
    }

    # ── 1. Récupérer données ─────────────────────────────────────────────────
    hour_data = REAL_HOUR_DIRECTION.get(sym, {}).get(hour_utc)
    dir10y    = _get_10y_direction(sym, hour_utc)
    bull10y   = _get_10y_bull_rate(sym, hour_utc)
    is_trans  = hour_utc in TRANSITION_HOURS.get(sym, set())

    macro_score, macro_note = _macro_score_for_sym(sym, macro)
    base["macro_score"] = macro_score
    base["macro_note"]  = macro_note

    # Score historique 10 ans: bull_rate centré sur 0.5
    hist_score = (bull10y - 0.5) * 2.0 if bull10y else 0.0
    base["hist_score"] = round(hist_score, 4)

    # ── 2. DUAL_LOSE : les 2 directions perdent historiquement ──────────────
    if hour_data and hour_data[0] == "DUAL_LOSE":
        # Cas AUDUSD H20: réduire lot 60% — ne jamais bloquer totalement
        base["lot_factor"]    = 0.40
        base["score_adj"]     = -0.05
        base["priority"]      = "MEDIUM"
        base["sources_aligned"] = 0
        base["reason"] = (f"{sym} H{hour_utc:02d}: historiquement les 2 directions "
                          f"perdent ({hour_data[4]} trades) — lot×0.40")
        base["rule"] = {"type": "dual_lose", "hour": hour_utc, "n": hour_data[4]}
        return base

    # ── 3. Identifier direction gagnante réelle ─────────────────────────────
    best_dir = None
    adv      = 0
    n_trades = 0

    if hour_data and hour_data[0] in ("BUY","SELL"):
        best_dir = hour_data[0]
        adv      = hour_data[1]
        n_trades = hour_data[4]

    # Si pas de données réelles mais stats 10 ans disent quelque chose
    if best_dir is None and dir10y in ("BUY","SELL"):
        best_dir = dir10y
        adv      = 10
        n_trades = _stats10y.get(sym, {}).get("hour_stats", {}).get(str(hour_utc), {}).get("n_samples", 0)

    # ── 4. Si aucune donnée fiable → NONE (pas d'interférence) ──────────────
    if best_dir is None:
        base["reason"] = f"{sym} H{hour_utc:02d}: données insuffisantes → pas d'interférence"
        return base

    # ── 5. Compter sources alignées ──────────────────────────────────────────
    sources_aligned = 0
    req_dir_str = "BUY" if want_buy else "SELL"

    # Source 1: trades réels
    if hour_data and hour_data[0] == req_dir_str:
        sources_aligned += 1

    # Source 2: stats 10 ans
    if dir10y == req_dir_str:
        sources_aligned += 1

    # Source 3: macro
    if req_dir_str == "BUY"  and macro_score >  0.15:
        sources_aligned += 1
    elif req_dir_str == "SELL" and macro_score < -0.15:
        sources_aligned += 1

    base["sources_aligned"] = sources_aligned

    # ── 6. Direction demandée = direction gagnante ? ─────────────────────────
    direction_is_correct = (req_dir_str == best_dir)

    # ── 7. Cas: direction CORRECTE ───────────────────────────────────────────
    if direction_is_correct:
        # Avantage fort (> $50/trade) → boost
        if adv >= 80 and n_trades >= 50:
            lot_boost = 1.25
            score_boost = 0.06
            priority = "HIGH"
        elif adv >= 30 and n_trades >= 20:
            lot_boost = 1.10
            score_boost = 0.03
            priority = "MEDIUM"
        else:
            lot_boost = 1.0
            score_boost = 0.01
            priority = "LOW"

        # Bonus macro
        if macro_score * (1 if req_dir_str == "BUY" else -1) > 0.3:
            lot_boost   = min(1.5, lot_boost + 0.10)
            score_boost = min(0.10, score_boost + 0.03)

        # Transition → prudence même si correct
        if is_trans:
            lot_boost = min(lot_boost, 0.85)

        base.update({
            "priority":       priority,
            "lot_factor":     round(lot_boost, 3),
            "score_adj":      round(score_boost, 3),
            "confidence":     round(min(0.93, 0.65 + adv / 500.0 + macro_score * 0.1), 4),
            "reason": (f"{sym} H{hour_utc:02d}: direction {req_dir_str} CORRECTE "
                       f"(avantage ${adv}/trade, {n_trades}t, macro={macro_score:+.2f}) "
                       f"→ lot×{lot_boost:.2f} score+{score_boost:.2f}"),
            "rule": {"type": "correct_direction", "advantage": adv, "n": n_trades},
        })
        return base

    # ── 8. Cas: direction INCORRECTE ─────────────────────────────────────────
    # L'EA demande la mauvaise direction selon l'historique

    # Est-ce que la macro confirme quand même la direction demandée?
    macro_confirms_req = (
        (req_dir_str == "BUY"  and macro_score >  0.35) or
        (req_dir_str == "SELL" and macro_score < -0.35)
    )

    # Est-ce que la macro confirme la bonne direction?
    macro_confirms_best = (
        (best_dir == "BUY"  and macro_score >  0.20) or
        (best_dir == "SELL" and macro_score < -0.20)
    )

    # Stats 10 ans confirment-elles la bonne direction?
    hist_confirms_best = (
        (best_dir == "BUY"  and bull10y > 0.55) or
        (best_dir == "SELL" and bull10y < 0.45)
    )

    # ── 8a. Macro forte confirme la direction demandée malgré stats contraires
    # → On garde la direction demandée mais on réduit le lot (macro > stats réels)
    if macro_confirms_req and not hist_confirms_best:
        base.update({
            "priority":          "MEDIUM",
            "direction_changed": False,
            "lot_factor":        0.80,
            "score_adj":         -0.02,
            "bad_dir_penalty":   0.02,
            "confidence":        0.58,
            "reason": (f"{sym} H{hour_utc:02d}: stats disent {best_dir} mais macro "
                       f"({macro_score:+.2f}) confirme {req_dir_str} → lot×0.80"),
            "rule": {"type": "macro_override_stats", "macro_score": macro_score},
        })
        return base

    # ── 8b. Avantage faible (< $15) ET macro neutre → laisser passer réduit
    if adv < 15 and abs(macro_score) < 0.20:
        base.update({
            "priority":        "LOW",
            "lot_factor":      0.85,
            "score_adj":       -0.01,
            "reason": (f"{sym} H{hour_utc:02d}: avantage faible ${adv} "
                       f"et macro neutre → lot×0.85 (pas de forçage)"),
            "rule": {"type": "weak_signal_no_force"},
        })
        return base

    # ── 8c. Direction clairement incorrecte (avantage > $25 ET confirmé) → FORCER
    # La "force" ici n'est pas un blocage: on retourne direction_changed=True
    # et le serveur adapte. Mais l'EA peut quand même trader avec lot réduit.
    force_it = (
        adv >= 25 and n_trades >= 15 and
        (hist_confirms_best or macro_confirms_best) and
        not macro_confirms_req
    )

    if force_it:
        new_dir_int  = 1 if best_dir == "BUY" else -1
        penalty      = min(0.08, adv / 1000.0)

        base.update({
            "priority":          "HIGH",
            "direction_changed": True,
            "force_direction":   best_dir,
            "final_direction":   new_dir_int,
            "bad_dir_penalty":   round(penalty, 4),
            "lot_factor":        0.90,  # Légèrement réduit car c'est un changement
            "score_adj":         round(-penalty, 4),
            "confidence":        round(min(0.85, 0.60 + adv/600.0), 4),
            "sources_aligned":   sources_aligned,
            "reason": (f"{sym} H{hour_utc:02d}: direction demandée={req_dir_str} "
                       f"mais {best_dir} gagne (avantage=${adv}/trade, {n_trades}t) "
                       f"→ FORCER {best_dir} (macro={macro_score:+.2f}, "
                       f"bull10y={bull10y:.2f})"),
            "rule": {
                "type": "direction_force",
                "best_dir": best_dir,
                "advantage": adv,
                "n": n_trades,
                "macro_score": macro_score,
                "hist_bull_rate": bull10y,
            },
        })
        return base

    # ── 8d. Pas de forçage mais réduire le lot ────────────────────────────────
    base.update({
        "priority":    "LOW",
        "lot_factor":  0.75,
        "score_adj":   -0.02,
        "reason": (f"{sym} H{hour_utc:02d}: direction {req_dir_str} sous-optimale "
                   f"(avantage ${adv} pour {best_dir}) → lot×0.75"),
        "rule": {"type": "suboptimal_direction", "best_dir": best_dir, "adv": adv},
    })
    return base


def get_forced_direction(symbol: str, hour_utc: int, macro: Optional[dict] = None) -> dict:
    """Retourne la direction forcée pour un actif/heure, sans context EA."""
    sym      = _normalize(symbol)
    hour_data = REAL_HOUR_DIRECTION.get(sym, {}).get(hour_utc)
    dir10y   = _get_10y_direction(sym, hour_utc)
    macro_score, macro_note = _macro_score_for_sym(sym, macro)

    best_dir = None
    if hour_data and hour_data[0] in ("BUY","SELL","DUAL_LOSE"):
        best_dir = hour_data[0]
    elif dir10y in ("BUY","SELL"):
        best_dir = dir10y

    return {
        "symbol":      sym,
        "hour_utc":    hour_utc,
        "best_dir":    best_dir,
        "advantage":   hour_data[1] if hour_data and len(hour_data) > 1 else 0,
        "n_trades":    hour_data[4] if hour_data and len(hour_data) > 4 else 0,
        "dir_10y":     dir10y,
        "bull_rate_10y": _get_10y_bull_rate(sym, hour_utc),
        "macro_score": macro_score,
        "macro_note":  macro_note,
        "is_transition": hour_utc in TRANSITION_HOURS.get(sym, set()),
    }


def get_summary(symbol: str) -> dict:
    """Résumé SMART_HOUR pour un actif (24 heures)."""
    sym   = _normalize(symbol)
    hours = []
    for h in range(24):
        fd = get_forced_direction(sym, h)
        hd = REAL_HOUR_DIRECTION.get(sym, {}).get(h)
        hours.append({
            "hour":       h,
            "best_dir":   fd["best_dir"],
            "advantage":  fd["advantage"],
            "n_trades":   fd["n_trades"],
            "dir_10y":    fd["dir_10y"],
            "bull_10y":   round(fd["bull_rate_10y"], 3),
            "is_transition": fd["is_transition"],
            "note": (hd[0] if hd else "no_data"),
        })

    # Meilleure et pire heure
    real_data = [(h, REAL_HOUR_DIRECTION.get(sym, {}).get(h)) for h in range(24)]
    best  = max((h for h, d in real_data if d and d[0] != "DUAL_LOSE"),
                key=lambda h: (REAL_HOUR_DIRECTION[sym][h][1]
                               if h in REAL_HOUR_DIRECTION.get(sym,{}) else 0),
                default=None)
    worst = next((h for h, d in real_data if d and d[0] == "DUAL_LOSE"), None)

    return {
        "symbol":      sym,
        "hours":       hours,
        "best_hour":   best,
        "worst_hour":  worst,
        "n_real_hours": sum(1 for h, d in real_data if d and d[0] != "DUAL_LOSE"),
    }


# ──────────────────────────────────────────────────────────────────────────────
# FONCTIONS ATTENDUES PAR HISTORICAL_STATS_ENGINE (via DFE)
# ──────────────────────────────────────────────────────────────────────────────

def get_market_hourly_bias(symbol: str, hour: int) -> dict:
    """Interface compatibilité avec HISTORICAL_STATS_ENGINE."""
    sym    = _normalize(symbol)
    dir10y = _get_10y_direction(sym, hour)
    bull   = _get_10y_bull_rate(sym, hour)
    n      = (_stats10y.get(sym, {})
              .get("hour_stats", {})
              .get(str(hour), {})
              .get("n_samples", 0))
    available = (dir10y is not None and n >= 50)
    return {
        "direction":  dir10y or "NEUTRAL",
        "bull_rate":  round(bull, 3),
        "confidence": round(abs(bull - 0.5) * 2.0, 3),
        "n":          n,
        "available":  available,
        "note":       f"{sym} H{hour:02d}: {'stats OK' if available else 'insuffisant'}",
    }


def is_transition_danger_zone(symbol: str, hour: int) -> tuple:
    sym       = _normalize(symbol)
    is_trans  = hour in TRANSITION_HOURS.get(sym, set())
    info      = None
    if is_trans:
        info = {
            "note": f"{sym} H{hour:02d}: zone de transition — direction change à H{hour+1:02d}",
            "reduce_lot": True,
        }
    return is_trans, info


# ──────────────────────────────────────────────────────────────────────────────
# TESTS
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("="*65)
    print("SMART_HOUR_ENGINE — Tests")
    print("="*65)

    fake_macro = {
        "dxy": 99.3, "vix": 18.0, "xau_bias": 0.1,
        "sp500_return": 0.3, "us10y": 3.75, "fear_greed": 55,
    }

    tests = [
        ("XAUUSDm", 20,  1, "XAU H20 BUY — direction correcte forte"),
        ("XAUUSDm", 20, -1, "XAU H20 SELL — direction incorrecte"),
        ("XAUUSDm", 13, -1, "XAU H13 SELL — correct avant transition"),
        ("XAGUSDm", 18,  1, "XAG H18 BUY — meilleure heure absol"),
        ("EURUSDm", 15,  1, "EUR H15 BUY — correct"),
        ("EURUSDm", 15, -1, "EUR H15 SELL — incorrect"),
        ("USDJPYm", 12, -1, "JPY H12 SELL — correct fort"),
        ("BTCUSDm",  1,  1, "BTC H01 BUY — correct fort"),
        ("AUDUSDm", 20,  1, "AUD H20 BUY — DUAL LOSE"),
        ("AUDUSDm", 17,  1, "AUD H17 BUY — correct fort"),
    ]

    for sym, h, d, desc in tests:
        r = smart_hour_decision(sym, h, d, fake_macro)
        print(f"\n{'✅' if r['direction_changed']==False else '🔄'} {desc}")
        print(f"   Priority={r['priority']} | lot×{r['lot_factor']:.2f} | "
              f"score_adj={r['score_adj']:+.3f} | sources={r['sources_aligned']}")
        print(f"   Reason: {r['reason'][:100]}")

    print("\n" + "="*65)
    print("✅ SMART_HOUR_ENGINE opérationnel")
