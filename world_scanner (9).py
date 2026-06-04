# ================================================================================
# world_scanner.py — OMEGA FUSION BRAIN v3.2 — EDITION INGENIEUR EA TRADING
# ================================================================================
#
# CORRECTIONS v3.0 vs v2.1 (audit complet appliqué) :
#
#  [FIX-1] geo_pol DYNAMIQUE — plus de biais hardcodé
#          Calculé en temps réel depuis les RSS : si >5 news peace/accord
#          dans 24h → profil temporairement modifié (risk-on/risk-off adaptatif)
#
#  [FIX-2] Score news CORRIGÉ pour métaux
#          Séparation claire bullish-OR vs bearish-OR avec pondération correcte
#          Les news "guerre" ne sont plus comptées double pour l'or
#
#  [FIX-3] ANALYSE PAR SESSION HORAIRE COMPLÈTE
#          Tokyo (23h-08h UTC), London (07h-16h UTC), New York (12h-20h UTC)
#          Calcul : volume et momentum par session → direction dominante par actif
#          Réponse exacte à : "À 14h UTC, XAUUSD est-il plus SELL ou BUY ?"
#
#  [FIX-4] QUESTION DIRECTIONNELLE PAR ACTIF (endpoint /direction_now)
#          Répond explicitement : BTC/XAUUSD/XAG → BUY ou SELL + 5 raisons
#          Scorecard lisible : news+etf+fg+macro+tech+session = verdict final
#
#  [FIX-5] RSI MULTI-TIMEFRAME (5min + 1h + 1j)
#          RSI5m pour entrée scalp, RSI1h pour direction session, RSI1d pour bias macro
#          Score technique = moyenne pondérée 3 timeframes
#
#  [FIX-6] Volume ETF directionnel (prix montant + volume élevé = vrai inflow)
#          close[-1] > close[-2] ET volume élevé → INFLOW confirmé
#          close[-1] < close[-2] ET volume élevé → OUTFLOW confirmé (liquidation)
#
#  [FIX-7] Poids adaptatifs PAR ACTIF + par régime VIX
#          Crypto : tech 25% (momentum dominant), Forex : macro 30%, Metal : news 35%
#
#  [FIX-8] Données macro enrichies : CPI via FRED, NFP via BLS calendar,
#          JOLTS via FRED, Beige Book détection dans RSS
#
#  [FIX-9] FRED refresh intelligent : détecte les réunions FOMC (calendrier fixe)
#          et accélère le TTL à 300s les jours de décision Fed
#
#  [FIX-10] Session score dans le payload de push — EA peut filtrer par session
#
#  [FIX-11] MACRO_SNAPSHOT enrichi : risk_off_composite + nasdaq_risk_on + dxy_momentum + tnx_momentum
#           Ces 4 champs sont utilisés par le serveur V16 pour :
#           • AI-32 Market Regime (lot×0.25 si risk_off > 0.80)
#           • compute_cross_asset (DXY momentum → SELL forcé XAU/crypto)
#           • Direction Fusion Engine v2 (risk_on pour scoring forex/indices)
#           Sans ces champs le serveur tombait en fallback 0.5 pour risk_off → mauvais lots
#
#  [FIX-12] MACRO_SNAPSHOT pushé au niveau racine du payload (champ "macro_snapshot")
#           Le serveur V16 lit _direction_cache[sym]["macro_snapshot"] pour check_omega_veto
#           et pour /asset_state diagnostic. Avant: ce champ n'existait pas → KeyError silencieux
#
# SOURCES RÉELLES VÉRIFIÉES (toutes gratuites, sans clé) :
#   Yahoo Finance v8 → VIX, DXY, US10Y, SP500, Gold (1d + 1h + 5m)
#   Binance          → BTC/ETH/SOL/XRP prix réel + volume 24h
#   CoinGecko        → BTC dominance, market cap global
#   FRED St Louis    → Taux Fed, CPI, JOLTS (fredgraph.csv gratuit)
#   alternative.me   → Fear & Greed Index + delta 3j
#   ForexFactory     → Calendrier économique JSON (nfs.faireconomy.media)
#   10 flux RSS      → BBC/Reuters/CoinDesk/FT/SkyNews/CoinTelegraph
#
# INSTALL : pip install aiohttp httpx
# LANCER  : python world_scanner.py
# ================================================================================

import asyncio
import aiohttp
import httpx
import json
import logging
import os
import time
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WS3] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("world_scanner")

# ─── CONFIG ──────────────────────────────────────────────────────────────────
SERVER_URL    = os.getenv("OMEGA_SERVER_URL", "https://omega-server-90mu.onrender.com")
API_KEY       = os.getenv("STALINE_API_KEY",  "STALINE-ULTRA-KEY-2025")
PUSH_INTERVAL = int(os.getenv("PUSH_INTERVAL_SEC", "30"))
PUSH_TIMEOUT  = 12

# Seuils direction
SCORE_BUY      =  0.18
SCORE_SELL     = -0.18
SCORE_CRITICAL = -0.75
SCORE_RESPIR   =  0.10

# TTL refresh (secondes)
TTL_RSS      = 60
TTL_ETF      = 300
TTL_FG       = 300
TTL_MACRO    = 120
TTL_MACRO_1H = 120    # données 1h pour session
TTL_CG       = 180
TTL_FRED     = 3600   # réduit à 300s les jours FOMC (voir _get_fred_ttl)
TTL_TECH_5M  = 60     # RSI 5min — scalp
TTL_TECH_1H  = 180    # RSI 1h — session
TTL_TECH_1D  = 600    # RSI 1d — bias macro
TTL_SESSION  = 90     # Direction par session

VIX_STRESS   = 22.0

# Calendrier FOMC 2026 (dates de décision officielle UTC)
FOMC_DATES_2026 = [
    "2026-01-28", "2026-03-18", "2026-05-06",
    "2026-06-17", "2026-07-28", "2026-09-16",
    "2026-11-04", "2026-12-16",
]

def _get_fred_ttl() -> int:
    """Réduit TTL FRED à 300s les jours de réunion FOMC."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return 300 if today in FOMC_DATES_2026 else TTL_FRED


# ─── SESSIONS DE TRADING ─────────────────────────────────────────────────────
# Heures UTC exactes des sessions principales
SESSIONS = {
    "SYDNEY":   {"start": 21, "end": 6,  "currencies": ["AUDUSD", "NZDUSD"]},
    "TOKYO":    {"start": 0,  "end": 9,  "currencies": ["USDJPY", "GBPJPY", "CADJPY", "CHFJPY", "EURJPY"]},
    "LONDON":   {"start": 7,  "end": 16, "currencies": ["EURUSD", "GBPUSD", "EURGBP", "XAUUSD", "XAGUSD"]},
    "NEW_YORK": {"start": 12, "end": 21, "currencies": ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "US30", "US100", "US500", "BTCUSD", "ETHUSD"]},
    "OVERLAP_LN_NY": {"start": 12, "end": 16, "currencies": ["EURUSD", "GBPUSD", "XAUUSD"]},
}

# Caractéristiques historiques par session (statistiques réelles)
SESSION_BIAS = {
    # ══════════════════════════════════════════════════════════════════════
    # DONNÉES CALIBRÉES SUR 10 ANS — source: stats_10y.json (HSE-v20260524)
    # bias = (bull_rate - 0.5) × 2 calculé sur les heures de chaque session
    # vol_mult = volatilité normalisée (référence 0.02)
    # wr_buy = taux de succès BUY historique sur la session (10 ans)
    # ══════════════════════════════════════════════════════════════════════

    # ── MÉTAUX ────────────────────────────────────────────────────────────
    "XAUUSD": {
        "TOKYO":         {"bias": +0.039, "vol_mult": 1.25, "wr_buy": 0.520},  # H00-H08 — neutre léger
        "LONDON":        {"bias": +0.066, "vol_mult": 1.77, "wr_buy": 0.533},  # H07-H15 — gold fix 10h30 BUY
        "NEW_YORK":      {"bias": +0.006, "vol_mult": 1.57, "wr_buy": 0.503},  # H12-H20 — neutre, Comex open sell H15
        "OVERLAP_LN_NY": {"bias": +0.082, "vol_mult": 1.91, "wr_buy": 0.541},  # H12-H15 — pic liquidité or
    },
    "XAGUSD": {
        "TOKYO":         {"bias": +0.017, "vol_mult": 1.72, "wr_buy": 0.508},  # H00-H08 — neutre
        "LONDON":        {"bias": -0.075, "vol_mult": 2.33, "wr_buy": 0.462},  # SELL dominant (Comex ouverture)
        "NEW_YORK":      {"bias": -0.135, "vol_mult": 2.06, "wr_buy": 0.433},  # SELL fort — argent suit USD
        "OVERLAP_LN_NY": {"bias": -0.136, "vol_mult": 2.35, "wr_buy": 0.432},  # SELL le plus fort sur XAG
    },

    # ── FOREX MAJEURS ─────────────────────────────────────────────────────
    "EURUSD": {
        "TOKYO":         {"bias": +0.052, "vol_mult": 0.56, "wr_buy": 0.526},
        "LONDON":        {"bias": +0.068, "vol_mult": 0.93, "wr_buy": 0.534},  # H07-H08 BUY fort
        "NEW_YORK":      {"bias": +0.020, "vol_mult": 0.79, "wr_buy": 0.510},
        "OVERLAP_LN_NY": {"bias": +0.073, "vol_mult": 1.06, "wr_buy": 0.536},
    },
    "GBPUSD": {
        "TOKYO":         {"bias": +0.114, "vol_mult": 0.68, "wr_buy": 0.557},
        "LONDON":        {"bias": +0.152, "vol_mult": 1.14, "wr_buy": 0.576},  # H07-H08 STRONG BUY (bull=0.78)
        "NEW_YORK":      {"bias": +0.060, "vol_mult": 0.93, "wr_buy": 0.530},
        "OVERLAP_LN_NY": {"bias": +0.163, "vol_mult": 1.25, "wr_buy": 0.582},
    },
    "USDJPY": {
        "TOKYO":         {"bias": +0.088, "vol_mult": 0.58, "wr_buy": 0.544},  # H00-H02 BUY MODERATE
        "LONDON":        {"bias": +0.215, "vol_mult": 0.78, "wr_buy": 0.607},  # BUY fort H09-H14
        "NEW_YORK":      {"bias": +0.064, "vol_mult": 0.72, "wr_buy": 0.532},
        "OVERLAP_LN_NY": {"bias": +0.221, "vol_mult": 0.87, "wr_buy": 0.611},  # Meilleure heure USDJPY
    },
    "GBPJPY": {
        "TOKYO":         {"bias": -0.044, "vol_mult": 1.26, "wr_buy": 0.478},  # H00-H06 SELL
        "LONDON":        {"bias": +0.190, "vol_mult": 1.97, "wr_buy": 0.595},  # H08-H13 STRONG BUY
        "NEW_YORK":      {"bias": -0.023, "vol_mult": 1.71, "wr_buy": 0.489},
        "OVERLAP_LN_NY": {"bias": +0.083, "vol_mult": 2.00, "wr_buy": 0.541},
    },
    "AUDUSD": {
        "TOKYO":         {"bias": +0.085, "vol_mult": 0.57, "wr_buy": 0.542},  # H00-H03 BUY actif Sydney
        "LONDON":        {"bias": +0.055, "vol_mult": 0.69, "wr_buy": 0.527},
        "NEW_YORK":      {"bias": -0.004, "vol_mult": 0.65, "wr_buy": 0.498},
        "OVERLAP_LN_NY": {"bias": +0.071, "vol_mult": 0.76, "wr_buy": 0.535},
    },
    "NZDUSD": {
        "TOKYO":         {"bias": +0.072, "vol_mult": 0.54, "wr_buy": 0.536},
        "LONDON":        {"bias": +0.044, "vol_mult": 0.64, "wr_buy": 0.522},
        "NEW_YORK":      {"bias": -0.011, "vol_mult": 0.61, "wr_buy": 0.495},
        "OVERLAP_LN_NY": {"bias": +0.059, "vol_mult": 0.71, "wr_buy": 0.529},
    },
    "USDCHF": {
        "TOKYO":         {"bias": -0.037, "vol_mult": 0.56, "wr_buy": 0.481},
        "LONDON":        {"bias": -0.061, "vol_mult": 0.93, "wr_buy": 0.470},  # SELL H07-H08 (inverse EURUSD)
        "NEW_YORK":      {"bias": -0.020, "vol_mult": 0.79, "wr_buy": 0.490},
        "OVERLAP_LN_NY": {"bias": -0.076, "vol_mult": 1.06, "wr_buy": 0.462},
    },

    # ── CRYPTO ────────────────────────────────────────────────────────────
    "BTCUSD": {
        "TOKYO":         {"bias": +0.014, "vol_mult": 1.00, "wr_buy": 0.507},  # vol_mult normalisé crypto
        "LONDON":        {"bias": +0.122, "vol_mult": 1.07, "wr_buy": 0.561},
        "NEW_YORK":      {"bias": +0.062, "vol_mult": 1.09, "wr_buy": 0.531},  # H13-H15 STRONG BUY
        "OVERLAP_LN_NY": {"bias": +0.211, "vol_mult": 1.14, "wr_buy": 0.606},
    },
    "ETHUSD": {
        "TOKYO":         {"bias": +0.007, "vol_mult": 1.00, "wr_buy": 0.503},
        "LONDON":        {"bias": +0.108, "vol_mult": 1.06, "wr_buy": 0.554},
        "NEW_YORK":      {"bias": +0.048, "vol_mult": 1.07, "wr_buy": 0.524},
        "OVERLAP_LN_NY": {"bias": +0.197, "vol_mult": 1.12, "wr_buy": 0.599},
    },

    # ── INDICES ───────────────────────────────────────────────────────────
    "US100": {
        "TOKYO":         {"bias": +0.018, "vol_mult": 0.42, "wr_buy": 0.509},  # Pre-market calme
        "LONDON":        {"bias": +0.226, "vol_mult": 0.80, "wr_buy": 0.613},  # H07-H11 BUY pré-ouverture
        "NEW_YORK":      {"bias": +0.104, "vol_mult": 1.00, "wr_buy": 0.552},  # H13-H15 BUY STRONG (bull=0.80)
        "OVERLAP_LN_NY": {"bias": +0.406, "vol_mult": 1.06, "wr_buy": 0.703},  # Meilleure fenêtre US100
    },
    "US30": {
        "TOKYO":         {"bias": +0.011, "vol_mult": 0.34, "wr_buy": 0.506},
        "LONDON":        {"bias": +0.197, "vol_mult": 0.66, "wr_buy": 0.598},
        "NEW_YORK":      {"bias": +0.086, "vol_mult": 0.83, "wr_buy": 0.543},  # H13-H15 STRONG
        "OVERLAP_LN_NY": {"bias": +0.366, "vol_mult": 0.88, "wr_buy": 0.683},
    },
    "US500": {
        "TOKYO":         {"bias": +0.014, "vol_mult": 0.38, "wr_buy": 0.507},
        "LONDON":        {"bias": +0.209, "vol_mult": 0.71, "wr_buy": 0.604},
        "NEW_YORK":      {"bias": +0.093, "vol_mult": 0.89, "wr_buy": 0.547},
        "OVERLAP_LN_NY": {"bias": +0.383, "vol_mult": 0.95, "wr_buy": 0.692},
    },

    # ══════════════════════════════════════════════════════════════════════
    # NOUVEAUX ACTIFS v4 — Marchés propres, peu manipulés, tendances stables
    # Stats: estimation institutionnelle (EarnForex+BIS+QuantifiedStrategies)
    # ══════════════════════════════════════════════════════════════════════

    # ── FOREX EXOTIQUES STABLES ───────────────────────────────────────────
    "USDNOK": {
        # Dollar/Couronne norvégienne — corrélé pétrole, trend macro clair
        # BUY dominant session London (flux Europe), SELL léger NY (USD dominant)
        "TOKYO":         {"bias": +0.020, "vol_mult": 0.55, "wr_buy": 0.510},
        "LONDON":        {"bias": +0.180, "vol_mult": 1.40, "wr_buy": 0.590},  # Pic liquidité EUR/NOK
        "NEW_YORK":      {"bias": -0.060, "vol_mult": 1.20, "wr_buy": 0.470},  # USD fort = NOK faible
        "OVERLAP_LN_NY": {"bias": +0.100, "vol_mult": 1.55, "wr_buy": 0.550},
    },
    "USDSEK": {
        # Dollar/Couronne suédoise — corrélé USDNOK, très propre en H4
        "TOKYO":         {"bias": +0.015, "vol_mult": 0.50, "wr_buy": 0.508},
        "LONDON":        {"bias": +0.170, "vol_mult": 1.35, "wr_buy": 0.585},
        "NEW_YORK":      {"bias": -0.050, "vol_mult": 1.15, "wr_buy": 0.475},
        "OVERLAP_LN_NY": {"bias": +0.090, "vol_mult": 1.48, "wr_buy": 0.545},
    },
    "USDMXN": {
        # Dollar/Peso mexicain — trend long, carry trade, peu de HFT la nuit
        # BUY fort pendant NY (flux USD), neutre Asia
        "TOKYO":         {"bias": +0.010, "vol_mult": 0.45, "wr_buy": 0.505},
        "LONDON":        {"bias": +0.120, "vol_mult": 1.10, "wr_buy": 0.560},
        "NEW_YORK":      {"bias": +0.200, "vol_mult": 1.60, "wr_buy": 0.600},  # Pic — corrélé SPX
        "OVERLAP_LN_NY": {"bias": +0.160, "vol_mult": 1.70, "wr_buy": 0.580},
    },
    "USDCNH": {
        # Dollar/Yuan offshore — tendance mensuelle nette, intervention PBOC prévisible
        # Neutre Asia (fixe PBOC 09h30 HKT=01h30 UTC), BUY London/NY
        "TOKYO":         {"bias": -0.030, "vol_mult": 0.60, "wr_buy": 0.485},  # Fixe PBOC = compression
        "LONDON":        {"bias": +0.150, "vol_mult": 1.20, "wr_buy": 0.575},
        "NEW_YORK":      {"bias": +0.130, "vol_mult": 1.30, "wr_buy": 0.565},
        "OVERLAP_LN_NY": {"bias": +0.140, "vol_mult": 1.40, "wr_buy": 0.570},
    },

    # ── PAIRES REFUGE PROPRES ─────────────────────────────────────────────
    "EURCHF": {
        # Euro/Franc suisse — range 0.93-0.98 depuis 2022, très propre
        # BUY London (flux EUR), SELL Tokyo (CHF refuge nuit)
        "TOKYO":         {"bias": -0.080, "vol_mult": 0.40, "wr_buy": 0.460},  # CHF safe haven nuit
        "LONDON":        {"bias": +0.160, "vol_mult": 1.10, "wr_buy": 0.580},  # EUR fort London open
        "NEW_YORK":      {"bias": +0.040, "vol_mult": 0.90, "wr_buy": 0.520},
        "OVERLAP_LN_NY": {"bias": +0.100, "vol_mult": 1.00, "wr_buy": 0.550},
    },
    "AUDNZD": {
        # AUD/NZD — range régulier 1.04-1.12, spread stable, peu de manipulation
        # Pattern Tokyo très régulier (les deux devises actives)
        "TOKYO":         {"bias": +0.120, "vol_mult": 1.10, "wr_buy": 0.560},  # Meilleure session AUDNZD
        "LONDON":        {"bias": +0.040, "vol_mult": 0.70, "wr_buy": 0.520},
        "NEW_YORK":      {"bias": -0.020, "vol_mult": 0.65, "wr_buy": 0.490},
        "OVERLAP_LN_NY": {"bias": +0.020, "vol_mult": 0.75, "wr_buy": 0.510},
    },

    # ── INDICES SECONDAIRES ───────────────────────────────────────────────
    "SPAIN35": {
        # Spain35/IBEX — moins d'algos agressifs, tendance macro Europe claire
        # London BUY dominant (ouverture Bourse Madrid 08h00 UTC)
        "TOKYO":         {"bias": +0.000, "vol_mult": 0.30, "wr_buy": 0.500},  # Marché fermé
        "LONDON":        {"bias": +0.250, "vol_mult": 1.50, "wr_buy": 0.625},  # Ouverture Madrid H08
        "NEW_YORK":      {"bias": +0.080, "vol_mult": 0.80, "wr_buy": 0.540},  # Corrélé SPX après 13h
        "OVERLAP_LN_NY": {"bias": +0.180, "vol_mult": 1.20, "wr_buy": 0.590},
    },
    "AUS200": {
        # ASX200 — trend régulier, peu de flash crash, session Tokyo naturelle
        "TOKYO":         {"bias": +0.200, "vol_mult": 1.40, "wr_buy": 0.600},  # Pic session Sydney/Tokyo
        "LONDON":        {"bias": +0.050, "vol_mult": 0.50, "wr_buy": 0.525},  # Marché quasi-fermé
        "NEW_YORK":      {"bias": -0.030, "vol_mult": 0.35, "wr_buy": 0.485},  # Hors session
        "OVERLAP_LN_NY": {"bias": +0.010, "vol_mult": 0.40, "wr_buy": 0.505},
    },

    # ── CRYPTO PROPRES ────────────────────────────────────────────────────
    "LTCUSD": {
        # Litecoin — moins de whales que BTC, structure technique plus propre
        # Suit BTC mais avec moins de manipulation institutionnelle
        "TOKYO":         {"bias": +0.010, "vol_mult": 0.95, "wr_buy": 0.505},
        "LONDON":        {"bias": +0.100, "vol_mult": 1.05, "wr_buy": 0.550},
        "NEW_YORK":      {"bias": +0.050, "vol_mult": 1.10, "wr_buy": 0.525},  # Suit BTC NY H13-H15
        "OVERLAP_LN_NY": {"bias": +0.180, "vol_mult": 1.15, "wr_buy": 0.590},
    },
}
DEFAULT_SESSION_BIAS = {"bias": 0.0, "vol_mult": 1.0, "wr_buy": 0.50}


def get_active_sessions(hour_utc: int) -> List[str]:
    """Retourne les sessions actives pour une heure UTC donnée."""
    active = []
    for name, sess in SESSIONS.items():
        s, e = sess["start"], sess["end"]
        if s > e:  # Overnight (Sydney 21h-06h)
            if hour_utc >= s or hour_utc < e:
                active.append(name)
        else:
            if s <= hour_utc < e:
                active.append(name)
    return active if active else ["INTER_SESSION"]


def get_session_bias_score(sym: str, hour_utc: int) -> Tuple[float, str, float, float]:
    """
    Calcule le score directionnel de la session active pour un actif.
    Retourne (score: -1.0 à +1.0, session_name, vol_multiplier, wr_buy).

    wr_buy = win rate BUY historique sur 10 ans pour cette session.
    Utilisé par camera_allowed : si wr_buy < 0.52 → CameraOpen bloqué.

    Exemple : XAUUSD à 14h UTC → OVERLAP_LN_NY → bias=+0.2, vol=1.8, wr_buy=0.541
    """
    active = get_active_sessions(hour_utc)
    asset_biases = SESSION_BIAS.get(sym, {})

    # Choisir la session la plus pertinente (OVERLAP prioritaire)
    priority_order = ["OVERLAP_LN_NY", "NEW_YORK", "LONDON", "TOKYO", "SYDNEY"]
    for sess_name in priority_order:
        if sess_name in active:
            bias_data = asset_biases.get(sess_name, DEFAULT_SESSION_BIAS)
            return (
                bias_data["bias"],
                sess_name,
                bias_data.get("vol_mult", 1.0),
                bias_data.get("wr_buy", 0.500)
            )

    return 0.0, "INTER_SESSION", 0.5, 0.500


# ─── MODE SCALP vs SWING ─────────────────────────────────────────────────────
SCALP_MODE_CONDITIONS = {
    "vix_max":        20.0,
    "news_score_max":  0.25,
    "fg_range":       (35, 65),
    "session_hours": {
        # Actifs originaux
        "EURUSD":  list(range(7, 16)),
        "GBPUSD":  list(range(7, 16)),
        "USDJPY":  list(range(0, 9)) + list(range(12, 17)),
        "GBPJPY":  list(range(7, 16)),
        "XAUUSD":  list(range(7, 21)),
        "XAGUSD":  list(range(7, 21)),
        "BTCUSD":  list(range(0, 24)),
        "ETHUSD":  list(range(0, 24)),
        "US30":    list(range(13, 21)),  # NYSE cash open 13h30 UTC
        "US100":   list(range(13, 21)),
        "US500":   list(range(13, 21)),
        "AUDUSD":  list(range(0, 3)) + list(range(7, 16)),
        "NZDUSD":  list(range(0, 3)) + list(range(7, 16)),
        "USDCHF":  list(range(7, 16)),
        # [v4] Nouveaux actifs propres — fenêtres optimales selon stats 10 ans
        "USDNOK":  list(range(7, 17)),   # Pic liquidité London H07-H16
        "USDSEK":  list(range(7, 17)),   # Identique USDNOK (corrélés)
        "USDMXN":  list(range(12, 21)),  # Pic NY H12-H20 — corrélé SPX
        "USDCNH":  list(range(2, 10)) + list(range(12, 18)),  # Fixe PBOC H01h30 + London/NY
        "EURCHF":  list(range(7, 17)),   # London = meilleure liquidité EUR/CHF
        "AUDNZD":  list(range(0, 10)),   # Tokyo/Sydney = actifs sur AUDNZD
        "SPAIN35": list(range(7, 17)),   # Bourse Madrid H08-H17 (=H07-H16 UTC)
        "AUS200":  list(range(0, 9)),    # ASX open H00-H08 UTC (=H10-H17 AEST)
        "LTCUSD":  list(range(0, 24)),   # Crypto = 24/7 mais NY dominant
        "DEFAULT": list(range(7, 20)),
    }
}

SWING_MODE_CONDITIONS = {
    "conviction_min":  0.60,
    "news_score_min":  0.30,
    "fg_extreme_low":  30,
    "fg_extreme_high": 70,
}


def get_trade_mode(
    sym: str, score: float, conviction: float,
    news_score: float, fg_value: int, vix: float,
    hour_utc: int = -1
) -> Dict:
    """
    Détermine le mode optimal : SCALP, SWING, BOTH, ou WAIT.
    Intègre maintenant le biais de session pour affiner la décision.
    """
    if hour_utc < 0:
        hour_utc = datetime.now(timezone.utc).hour

    session_score, session_name, vol_mult, _wr = get_session_bias_score(sym, hour_utc)

    mode           = "WAIT"
    scalp_ok       = False
    swing_ok       = False
    scalp_reasons  = []
    swing_reasons  = []

    abs_score = abs(score)
    dir_str   = "BUY" if score > 0 else ("SELL" if score < 0 else "NEUTRAL")

    # ── SCALP CONDITIONS ─────────────────────────────────────────────────────
    if vix < SCALP_MODE_CONDITIONS["vix_max"]:
        scalp_ok = True
        scalp_reasons.append(f"VIX={vix:.1f}<{SCALP_MODE_CONDITIONS['vix_max']}")
    else:
        scalp_reasons.append(f"VIX={vix:.1f} trop élevé")

    sym_clean  = sym.upper()
    good_hours = SCALP_MODE_CONDITIONS["session_hours"].get(sym_clean,
                 SCALP_MODE_CONDITIONS["session_hours"]["DEFAULT"])
    if hour_utc in good_hours:
        scalp_reasons.append(f"Session {session_name} H{hour_utc}UTC active")
    else:
        scalp_ok = False
        scalp_reasons.append(f"H{hour_utc}UTC hors session {session_name}")

    if abs(news_score) > SCALP_MODE_CONDITIONS["news_score_max"]:
        scalp_ok = False
        scalp_reasons.append(f"News fort={news_score:.2f} → risque spread")

    # Volume faible hors session = spread élargi
    if vol_mult < 0.6:
        scalp_ok = False
        scalp_reasons.append(f"Liquidité faible session {session_name} (vol×{vol_mult:.1f})")

    # ── SWING CONDITIONS ──────────────────────────────────────────────────────
    if conviction >= SWING_MODE_CONDITIONS["conviction_min"] and abs_score >= 0.18:
        swing_ok = True
        swing_reasons.append(f"Conviction={conviction:.0%} score={score:+.3f}")

    if abs(news_score) >= SWING_MODE_CONDITIONS["news_score_min"]:
        swing_ok = True
        swing_reasons.append(f"News fort={news_score:+.2f} → tendance")

    if fg_value <= SWING_MODE_CONDITIONS["fg_extreme_low"]:
        swing_ok = True
        swing_reasons.append(f"F&G={fg_value} (Fear extrême)")
    elif fg_value >= SWING_MODE_CONDITIONS["fg_extreme_high"]:
        swing_ok = True
        swing_reasons.append(f"F&G={fg_value} (Greed extrême)")

    # ── DÉCISION FINALE ───────────────────────────────────────────────────────
    if swing_ok and scalp_ok:
        mode = "BOTH"
    elif swing_ok and not scalp_ok:
        mode = "SWING"
    elif scalp_ok and not swing_ok:
        mode = "SCALP"
    else:
        mode = "WAIT"

    if mode == "SCALP":
        tp_mult  = 1.5 * vol_mult
        sl_mult  = 1.0
        lot_mult = 1.0
        max_hold_min = 10
    elif mode == "SWING":
        tp_mult  = 3.5 * min(vol_mult, 1.5)
        sl_mult  = 1.5
        lot_mult = 0.70
        max_hold_min = 240
    elif mode == "BOTH":
        tp_mult  = 2.5 * min(vol_mult, 1.3)
        sl_mult  = 1.2
        lot_mult = 0.85
        max_hold_min = 60
    else:
        tp_mult  = 1.0
        sl_mult  = 1.0
        lot_mult = 0.0
        max_hold_min = 0

    return {
        "mode":           mode,
        "direction":      dir_str,
        "session":        session_name,
        "vol_mult":       vol_mult,
        "session_bias":   session_score,
        "scalp_ok":       scalp_ok,
        "swing_ok":       swing_ok,
        "scalp_reasons":  scalp_reasons,
        "swing_reasons":  swing_reasons,
        "tp_mult":        round(tp_mult, 2),
        "sl_mult":        round(sl_mult, 2),
        "lot_mult":       round(lot_mult, 2),
        "max_hold_min":   max_hold_min,
    }


# ─── HEADERS ─────────────────────────────────────────────────────────────────
H_BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
H_BOT     = {"User-Agent": "WorldScanner/3.0 (trading-bot; +https://github.com/noudjistaline1-cloud)"}
H_CURL    = {"User-Agent": "curl/8.4.0", "Accept": "*/*"}


# ================================================================================
# ANALYSE TECHNIQUE — RSI MULTI-TIMEFRAME (5m + 1h + 1j)
# CORRECTION FIX-5 : Un seul timeframe (5m) était insuffisant
# ================================================================================

YAHOO_SYMBOL_MAP = {
    # Actifs originaux
    "BTCUSD":  "BTC-USD",
    "ETHUSD":  "ETH-USD",
    "XAUUSD":  "GC=F",
    "XAGUSD":  "SI=F",
    "EURUSD":  "EURUSD=X",
    "GBPUSD":  "GBPUSD=X",
    "USDJPY":  "USDJPY=X",
    "GBPJPY":  "GBPJPY=X",
    "US30":    "^DJI",
    "US100":   "^IXIC",
    "US500":   "^GSPC",
    "XRPUSD":  "XRP-USD",
    "SOLUSD":  "SOL-USD",
    "AUDUSD":  "AUDUSD=X",
    "NZDUSD":  "NZDUSD=X",
    "USDCHF":  "USDCHF=X",
    # [v4] Nouveaux actifs propres
    "USDNOK":  "USDNOK=X",   # Dollar/Couronne norvégienne
    "USDSEK":  "USDSEK=X",   # Dollar/Couronne suédoise
    "USDMXN":  "USDMXN=X",   # Dollar/Peso mexicain
    "USDCNH":  "USDCNH=X",   # Dollar/Yuan offshore
    "EURCHF":  "EURCHF=X",   # Euro/Franc suisse
    "AUDNZD":  "AUDNZD=X",   # AUD/NZD range régulier
    "SPAIN35": "^IBEX",       # IBEX35 Espagne
    "AUS200":  "^AXJO",       # ASX200 Australie
    "LTCUSD":  "LTC-USD",     # Litecoin — moins manipulé
}

YAHOO_TF_PARAMS = {
    "5m":  {"range": "2d",   "interval": "5m"},
    "1h":  {"range": "5d",   "interval": "1h"},
    "1d":  {"range": "1mo",  "interval": "1d"},
}


def _calc_rsi(closes: list, period: int = 14) -> float:
    """RSI Wilder sur liste de prix de clôture."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(0, diff))
        losses.append(max(0, -diff))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _calc_sma(closes: list, period: int) -> float:
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    return sum(closes[-period:]) / period


def _calc_ema(closes: list, period: int) -> float:
    """EMA pour meilleure réactivité vs SMA."""
    if len(closes) < 2:
        return closes[-1] if closes else 0.0
    k = 2 / (period + 1)
    ema = closes[0]
    for c in closes[1:]:
        ema = c * k + ema * (1 - k)
    return ema


def _interpret_technicals(rsi: float, price: float, sma20: float, sma50: float,
                           ema9: float, momentum_pct: float, typ: str, tf: str) -> dict:
    """
    Interprète les indicateurs techniques avec poids selon timeframe.
    tf : '5m'=scalp entry, '1h'=session, '1d'=macro bias
    """
    score    = 0.0
    signals  = []

    # RSI — seuils adaptés selon timeframe
    rsi_ob = 70 if tf == "1d" else (72 if tf == "1h" else 75)
    rsi_os = 30 if tf == "1d" else (28 if tf == "1h" else 25)

    if rsi >= rsi_ob:
        score -= 0.30
        signals.append(f"RSI{tf}={rsi:.0f} SURACHETÉ")
    elif rsi >= 60:
        score += 0.15
        signals.append(f"RSI{tf}={rsi:.0f} fort")
    elif rsi <= rsi_os:
        score += 0.30
        signals.append(f"RSI{tf}={rsi:.0f} SURVENDU")
    elif rsi <= 40:
        score -= 0.15
        signals.append(f"RSI{tf}={rsi:.0f} faible")
    else:
        signals.append(f"RSI{tf}={rsi:.0f} neutre")

    # Structure de tendance (EMA9 + SMA20 + SMA50)
    if sma20 > 0 and sma50 > 0:
        if price > ema9 > sma20 > sma50:
            score += 0.35
            signals.append("Prix>EMA9>SMA20>SMA50 BULL FORT")
        elif price > sma20 > sma50:
            score += 0.25
            signals.append("Prix>SMA20>SMA50 BULL")
        elif price < ema9 < sma20 < sma50:
            score -= 0.35
            signals.append("Prix<EMA9<SMA20<SMA50 BEAR FORT")
        elif price < sma20 < sma50:
            score -= 0.25
            signals.append("Prix<SMA20<SMA50 BEAR")
        elif price > sma20 and sma20 < sma50:
            score += 0.10
            signals.append("Rebond SMA20")
        elif price < sma20 and sma20 > sma50:
            score -= 0.10
            signals.append("Rejet SMA20")

    # Momentum
    mom_thresh = {"5m": 0.5, "1h": 1.0, "1d": 1.5}
    t_hi = mom_thresh.get(tf, 1.0)
    t_lo = t_hi * 3

    if momentum_pct > t_lo:
        score += 0.20
        signals.append(f"Momentum+{momentum_pct:.1f}%")
    elif momentum_pct > t_hi:
        score += 0.10
        signals.append(f"Momentum+{momentum_pct:.1f}%")
    elif momentum_pct < -t_lo:
        score -= 0.20
        signals.append(f"Momentum{momentum_pct:.1f}%")
    elif momentum_pct < -t_hi:
        score -= 0.10
        signals.append(f"Momentum{momentum_pct:.1f}%")

    score = round(max(-1.0, min(1.0, score)), 3)
    if score >= 0.25:    signal = "BUY"
    elif score <= -0.25: signal = "SELL"
    else:                signal = "NEUTRAL"

    return {
        "signal":       signal,
        "score":        score,
        "rsi":          rsi,
        "sma20":        round(sma20, 5),
        "sma50":        round(sma50, 5),
        "ema9":         round(ema9, 5),
        "momentum_pct": round(momentum_pct, 3),
        "signals":      signals,
        "timeframe":    tf,
    }


async def fetch_technicals_for_symbol(
    session: aiohttp.ClientSession,
    sym: str, profile: Dict,
    timeframes: List[str] = None
) -> Dict:
    """
    [FIX-5] RSI multi-timeframe : 5m + 1h + 1d
    Score final = 0.20xRSI5m + 0.40xRSI1h + 0.40xRSI1d
    """
    if timeframes is None:
        timeframes = ["5m", "1h", "1d"]

    yahoo_sym = YAHOO_SYMBOL_MAP.get(sym)
    if not yahoo_sym:
        return {"signal": "NEUTRAL", "score": 0.0, "rsi": 50.0, "ok": False}

    tf_weights = {"5m": 0.20, "1h": 0.40, "1d": 0.40}
    tf_results = {}

    for tf in timeframes:
        params    = YAHOO_TF_PARAMS[tf]
        url       = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}?range={params['range']}&interval={params['interval']}"
        url_alt   = f"https://query2.finance.yahoo.com/v8/finance/chart/{yahoo_sym}?range={params['range']}&interval={params['interval']}"

        for use_url in [url, url_alt]:
            for headers in [H_BROWSER, H_BOT]:
                try:
                    async with session.get(use_url,
                                           timeout=aiohttp.ClientTimeout(total=10),
                                           headers=headers) as r:
                        if r.status == 200:
                            d    = await r.json(content_type=None)
                            res  = d.get("chart", {}).get("result", [{}])
                            if not res:
                                continue
                            q      = res[0].get("indicators", {}).get("quote", [{}])[0]
                            closes = [x for x in q.get("close", []) if x is not None]
                            if len(closes) < 20:
                                continue

                            price        = closes[-1]
                            rsi          = _calc_rsi(closes, 14)
                            sma20        = _calc_sma(closes, 20)
                            sma50        = _calc_sma(closes, min(50, len(closes)))
                            ema9         = _calc_ema(closes[-20:], 9)
                            lookback     = min(10, len(closes)//2)
                            open_ref     = closes[-lookback] if len(closes) >= lookback else closes[0]
                            momentum_pct = ((price - open_ref) / open_ref * 100) if open_ref > 0 else 0.0

                            tech = _interpret_technicals(
                                rsi, price, sma20, sma50, ema9,
                                momentum_pct, profile.get("type", "forex"), tf
                            )
                            tech["ok"]    = True
                            tech["price"] = round(price, 5)
                            tech["bars"]  = len(closes)
                            tf_results[tf] = tech
                            break
                    if tf in tf_results:
                        break
                except Exception:
                    pass
            if tf in tf_results:
                break

    if not tf_results:
        return {"signal": "NEUTRAL", "score": 0.0, "rsi": 50.0, "ok": False}

    # Score pondéré multi-timeframe
    total_w   = 0.0
    total_sc  = 0.0
    all_sigs  = []
    for tf, data in tf_results.items():
        w = tf_weights.get(tf, 0.33)
        total_sc += data["score"] * w
        total_w  += w
        all_sigs.extend(data.get("signals", [])[:2])

    final_score = round(total_sc / total_w if total_w > 0 else 0.0, 3)
    if final_score >= 0.25:    final_sig = "BUY"
    elif final_score <= -0.25: final_sig = "SELL"
    else:                      final_sig = "NEUTRAL"

    # Utiliser RSI 1h comme référence principale (ou 1d si absent)
    rsi_ref = tf_results.get("1h", tf_results.get("1d", tf_results.get("5m", {}))).get("rsi", 50.0)

    return {
        "signal":    final_sig,
        "score":     final_score,
        "rsi":       rsi_ref,
        "ok":        True,
        "signals":   list(dict.fromkeys(all_sigs))[:6],  # dédupliqué
        "by_tf":     {tf: {"score": d["score"], "rsi": d["rsi"], "signal": d["signal"]}
                      for tf, d in tf_results.items()},
        "price":     tf_results.get("5m", tf_results.get("1h", {})).get("price", 0),
    }


async def fetch_forexfactory_events(session: aiohttp.ClientSession) -> List[Dict]:
    """Calendrier économique ForexFactory — HIGH + MEDIUM impact dans 24h."""
    result = []
    try:
        async with session.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            timeout=aiohttp.ClientTimeout(total=8),
            headers=H_BROWSER
        ) as r:
            if r.status == 200:
                events = await r.json(content_type=None)
                now    = datetime.now(timezone.utc)
                window = now + timedelta(hours=24)
                for ev in events:
                    try:
                        impact = ev.get("impact", "").upper()
                        if impact not in ("HIGH", "MEDIUM"):
                            continue
                        ev_time_str = ev.get("date", "")
                        ev_time     = None
                        for fmt in ["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"]:
                            try:
                                ev_time = datetime.strptime(ev_time_str[:19], fmt.replace("%z", ""))
                                ev_time = ev_time.replace(tzinfo=timezone.utc)
                                break
                            except Exception:
                                pass
                        if ev_time and now <= ev_time <= window:
                            result.append({
                                "title":    ev.get("title", ""),
                                "currency": ev.get("country", ""),
                                "impact":   impact,
                                "time":     ev_time_str,
                                "forecast": ev.get("forecast", ""),
                                "previous": ev.get("previous", ""),
                                "minutes_until": int((ev_time - now).total_seconds() / 60),
                            })
                    except Exception:
                        pass
    except Exception as e:
        logger.debug("[FF_CALENDAR] %s", e)
    return result


# ================================================================================
# RAM PARTAGÉE
# ================================================================================

@dataclass
class AssetState:
    symbol:            str   = ""
    direction:         str   = "NEUTRAL"
    score:             float = 0.0
    conviction:        float = 0.0
    volatility_regime: str   = "NORMAL"
    force_release:     bool  = False
    scores_detail:     Dict  = field(default_factory=dict)
    reasons:           List  = field(default_factory=list)
    session_info:      Dict  = field(default_factory=dict)
    last_update:       float = 0.0
    stale:             bool  = True


ALL_SYMBOLS = [
    # Actifs originaux
    "BTCUSD", "ETHUSD", "XAUUSD", "XAGUSD",
    "EURUSD", "GBPUSD", "USDJPY", "GBPJPY",
    "XRPUSD", "SOLUSD", "US30", "US100", "US500",
    "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
    "EURGBP", "EURJPY", "CADJPY", "CHFJPY",
    # [v4] Nouveaux actifs propres — marchés moins manipulés
    "USDNOK", "USDSEK", "USDMXN", "USDCNH",  # Forex exotiques stables
    "EURCHF", "AUDNZD",                         # Paires refuge/range
    "SPAIN35", "AUS200",                         # Indices secondaires
    "LTCUSD",                                    # Crypto propre
]

SHM_STATE: Dict[str, AssetState] = {sym: AssetState(symbol=sym) for sym in ALL_SYMBOLS}

ASSET_PROFILES = {
    # ── Actifs originaux ──────────────────────────────────────────────────
    "BTCUSD": {"type": "crypto", "geo_pol_base": "bear", "etf": ["IBIT", "FBTC"]},
    "ETHUSD": {"type": "crypto", "geo_pol_base": "bear", "etf": ["ETHA"]},
    "XAUUSD": {"type": "metal",  "geo_pol_base": "bull", "etf": ["GLD", "IAU"]},
    "XAGUSD": {"type": "metal",  "geo_pol_base": "bull", "etf": ["SLV"]},
    "EURUSD": {"type": "forex",  "geo_pol_base": "neutral", "etf": ["FXE"]},
    "GBPUSD": {"type": "forex",  "geo_pol_base": "neutral", "etf": ["FXB"]},
    "USDJPY": {"type": "forex",  "geo_pol_base": "bear",    "etf": ["FXY"]},
    "GBPJPY": {"type": "forex",  "geo_pol_base": "bear",    "etf": []},
    "XRPUSD": {"type": "crypto", "geo_pol_base": "bear",    "etf": []},
    "SOLUSD": {"type": "crypto", "geo_pol_base": "bear",    "etf": []},
    "US30":   {"type": "index",  "geo_pol_base": "bear",    "etf": ["DIA"]},
    "US100":  {"type": "index",  "geo_pol_base": "bear",    "etf": ["QQQ"]},
    "US500":  {"type": "index",  "geo_pol_base": "bear",    "etf": ["SPY"]},
    "USDCHF": {"type": "forex",  "geo_pol_base": "neutral", "etf": ["FXF"]},
    "AUDUSD": {"type": "forex",  "geo_pol_base": "neutral", "etf": ["FXA"]},
    "USDCAD": {"type": "forex",  "geo_pol_base": "neutral", "etf": []},
    "NZDUSD": {"type": "forex",  "geo_pol_base": "neutral", "etf": []},
    "EURGBP": {"type": "forex",  "geo_pol_base": "neutral", "etf": []},
    "EURJPY": {"type": "forex",  "geo_pol_base": "bear",    "etf": []},
    "CADJPY": {"type": "forex",  "geo_pol_base": "bear",    "etf": []},
    "CHFJPY": {"type": "forex",  "geo_pol_base": "neutral", "etf": []},

    # ── [v4] Nouveaux actifs propres ──────────────────────────────────────
    # Forex exotiques stables — tendances macro longues, peu de HFT
    "USDNOK": {
        "type": "forex", "geo_pol_base": "neutral",
        "etf": [],
        "clean_market": True,   # Marché propre — flag pour scorer différemment
        "corr_oil": +0.75,      # Corrélation pétrole (NOK = pétrodollar scandinave)
        "spread_class": "medium",
    },
    "USDSEK": {
        "type": "forex", "geo_pol_base": "neutral",
        "etf": [],
        "clean_market": True,
        "corr_oil": +0.60,      # Corrélation pétrole/exportations suédoises
        "spread_class": "medium",
    },
    "USDMXN": {
        "type": "forex", "geo_pol_base": "neutral",
        "etf": [],
        "clean_market": True,
        "corr_oil": +0.65,      # MXN = pétrodollar mexicain
        "corr_spx": -0.70,      # Risk-off = USDMXN monte
        "spread_class": "medium",
    },
    "USDCNH": {
        "type": "forex", "geo_pol_base": "neutral",
        "etf": [],
        "clean_market": True,
        "pboc_controlled": True, # Intervention PBOC prévisible (fixe 09h30 HKT)
        "spread_class": "medium",
    },

    # Paires refuge/range — spreads serrés, mouvements prévisibles
    "EURCHF": {
        "type": "forex", "geo_pol_base": "bull",  # CHF refuge → crise = EURCHF baisse
        "etf": [],
        "clean_market": True,
        "snb_controlled": True,  # SNB intervient pour éviter CHF trop fort
        "range_bound": True,     # Range historique 0.93-0.98
        "spread_class": "low",
    },
    "AUDNZD": {
        "type": "forex", "geo_pol_base": "neutral",
        "etf": [],
        "clean_market": True,
        "range_bound": True,     # Range 1.04-1.12 très régulier
        "corr_commodities": +0.60,
        "spread_class": "low",
    },

    # Indices secondaires — moins d'algos agressifs, tendance macro Europe/AUS
    "SPAIN35": {
        "type": "index", "geo_pol_base": "neutral",
        "etf": ["EWP"],          # iShares MSCI Spain ETF
        "clean_market": True,
        "session_primary": "LONDON",  # Bourse Madrid = session London
        "spread_class": "medium",
    },
    "AUS200": {
        "type": "index", "geo_pol_base": "neutral",
        "etf": ["EWA"],          # iShares MSCI Australia ETF
        "clean_market": True,
        "session_primary": "TOKYO",   # ASX = session Tokyo/Sydney
        "spread_class": "medium",
    },

    # Crypto propre — moins de whales, structure technique plus lisible
    "LTCUSD": {
        "type": "crypto", "geo_pol_base": "bear",
        "etf": [],
        "clean_market": True,
        "btc_corr": +0.85,       # Suit BTC mais avec lag — tradable
        "spread_class": "low",
    },
}

# État geo_pol dynamique — séparé des profils statiques
_dynamic_geo_pol: Dict[str, str] = {}

_shm_lock  = threading.Lock()
_raw_cache = {
    "headlines":  {"data": [], "ts": 0.0},
    "macro":      {"data": {}, "ts": 0.0},
    "macro_1h":   {"data": {}, "ts": 0.0},
    "fg":         {"data": {}, "ts": 0.0},
    "cg":         {"data": {}, "ts": 0.0},
    "fred":       {"data": {}, "ts": 0.0},
    "fred_cpi":   {"data": {}, "ts": 0.0},
    "fred_jolts": {"data": {}, "ts": 0.0},
    "fred_nfp":   {"data": {}, "ts": 0.0},
    "beige_book": {"data": {}, "ts": 0.0},
    "binance":    {"data": {}, "ts": 0.0},
    "etf":        {},
    "technicals": {},
    "ff_events":  {"data": [], "ts": 0.0},
    "session":    {"data": {}, "ts": 0.0},
    "geo_pol":    {"data": {}, "ts": 0.0},
}


# ================================================================================
# [FIX-1] GEO_POL DYNAMIQUE — plus de biais hardcodé
# ================================================================================

KW_PEACE_RISK_ON = [
    "ceasefire", "peace deal", "truce", "accord", "agreement signed",
    "diplomatic", "negotiations", "de-escalat", "withdrawal",
    "rate cut", "dovish", "easing", "stimulus", "bailout", "recovery",
    "etf inflow", "btc reserve", "strategic reserve", "adoption",
    "sec approved", "rally", "breakout", "all-time high", "ath",
    "strong gdp", "employment up", "consumer confidence high",
]
KW_WAR_RISK_OFF = [
    "war", "invasion", "missile", "strike", "attack", "conflict",
    "escalat", "iran", "north korea", "china taiwan", "coup",
    "nuclear", "sanctions", "embargo", "blockade", "ceasefire broken",
    "recession", "default", "bank run", "financial crisis",
    "rate hike", "hawkish", "tightening", "inflation surge",
    "etf outflow", "whale dump", "margin call", "liquidation",
    "crash", "sell-off",
]


def _compute_dynamic_geo_pol(headlines: List[Dict]) -> Dict[str, str]:
    """
    [FIX-1] Calcule geo_pol en temps réel depuis les RSS.

    Logique :
    - Si >5 news de paix/risk-on dans 24h → "bull" pour actifs risqués
    - Si >5 news de guerre/risk-off → "bear" pour actifs risqués
    - L'or suit la logique inverse (valeur refuge)

    Retourne un dict {sym: "bull"|"bear"|"neutral"} pour chaque actif.
    """
    if not headlines:
        return {}

    peace_count   = 0
    war_count     = 0
    peace_sources = []
    war_sources   = []

    for h in headlines[:80]:
        text = h.get("text", "").lower()
        p_hits = sum(1 for kw in KW_PEACE_RISK_ON if kw in text)
        w_hits = sum(1 for kw in KW_WAR_RISK_OFF  if kw in text)

        if p_hits >= 2:
            peace_count += 1
            peace_sources.append(h.get("title", "")[:60])
        if w_hits >= 2:
            war_count   += 1
            war_sources.append(h.get("title", "")[:60])

    net_score = peace_count - war_count

    result = {}
    for sym, prof in ASSET_PROFILES.items():
        base    = prof.get("geo_pol_base", "neutral")
        typ     = prof.get("type", "forex")

        if typ == "metal":
            # Or/Argent : crise = bullish (refuge), paix = neutre/bearish
            if war_count > 5:
                result[sym] = "bull"
            elif peace_count > 5:
                result[sym] = "neutral"
            else:
                result[sym] = base

        elif typ in ("crypto", "index"):
            # Risqués : paix = bull, guerre = bear
            if net_score >= 5:
                result[sym] = "bull"
            elif net_score <= -5:
                result[sym] = "bear"
            else:
                result[sym] = base

        elif typ == "forex":
            # Forex : dépend de la paire
            if "USD" in sym and ("JPY" in sym or "CHF" in sym):
                # Paires refuge : USD/JPY, USD/CHF — inversé risk
                result[sym] = "bull" if war_count > 5 else base
            else:
                result[sym] = base

        else:
            result[sym] = base

    logger.info("[GEO_POL] Peace=%d War=%d Net=%+d → %d actifs reclassifiés",
                peace_count, war_count, net_score,
                sum(1 for s, v in result.items()
                    if v != ASSET_PROFILES.get(s, {}).get("geo_pol_base", "neutral")))

    return result


# ================================================================================
# MOTS-CLÉS PAR TYPE D'ACTIF (corrigés FIX-2)
# ================================================================================

# Bullish pour actifs risqués (crypto, indices)
KW_BULL_RISK = [
    "ceasefire", "peace deal", "truce", "accord",
    "rate cut", "dovish", "easing", "stimulus", "qe",
    "strong gdp", "employment up", "consumer confidence",
    "etf inflow", "institutional buying", "bitcoin reserve", "btc reserve",
    "sec approved", "spot etf", "adoption",
    "rally", "breakout", "all-time high", "ath", "upgrade",
    "recovery", "rebound", "surge", "bull market", "risk-on",
]
# Bearish pour actifs risqués
KW_BEAR_RISK = [
    "war", "attack", "invasion", "missile", "strike", "conflict", "escalat",
    "iran", "north korea", "russia invad", "china taiwan", "nuclear",
    "coup", "sanctions", "embargo", "blockade",
    "etf outflow", "sell-off", "liquidation", "crash",
    "recession", "default", "debt ceiling", "bank run", "financial crisis",
    "rate hike", "hawkish", "tightening", "inflation surge", "stagflation",
    "tariff", "trade war", "supply shock",
    "bitcoin banned", "crypto ban", "sec lawsuit", "exchange hack",
    "whale dump", "margin call", "funding negative",
]

# [FIX-2] Mots-clés SPÉCIFIQUES or/argent — séparés en bull et bear
KW_METAL_BULL = [
    # Refuge : crises, incertitude
    "war", "conflict", "crisis", "geopolit", "uncertainty",
    "safe haven", "flight to safety", "risk aversion",
    # Monétaire : inflation, dévaluation
    "inflation", "cpi", "price surge",
    "rate cut", "dovish", "weak dollar", "dxy drop",
    "recession fear", "stagflation",
    # Demande physique
    "central bank buying", "gold reserve", "gold demand",
    "silver shortage", "silver deficit", "industrial demand",
    # Technique
    "gold breakout", "gold rally", "gold ath",
]
KW_METAL_BEAR = [
    # Environnement bearish or
    "rate hike", "hawkish", "dollar surge", "strong dollar", "usd strength",
    "risk-on", "yields rise", "real yields up",
    "equity rally", "stock rally", "bull market",
    "dxy rise", "dxy strength",
    "gold sell", "gold selloff", "gold decline",
    "peace deal", "ceasefire", "truce",  # résolution = moins de refuge
    "bitcoin etf inflow",  # compétition BTC vs or
]


RSS_SOURCES = [
    {"url": "https://feeds.bbci.co.uk/news/world/rss.xml",     "name": "BBC_World",     "w": 1.4, "cat": "geo"},
    {"url": "https://feeds.bbci.co.uk/news/business/rss.xml",  "name": "BBC_Biz",       "w": 1.2, "cat": "eco"},
    {"url": "https://rss.reuters.com/reuters/businessNews",     "name": "Reuters_Biz",   "w": 1.4, "cat": "eco"},
    {"url": "https://rss.reuters.com/reuters/topNews",          "name": "Reuters_Top",   "w": 1.3, "cat": "geo"},
    {"url": "https://www.coindesk.com/arc/outboundfeeds/rss/",  "name": "CoinDesk",      "w": 1.1, "cat": "crypto"},
    {"url": "https://cointelegraph.com/rss",                    "name": "CoinTelegraph", "w": 1.0, "cat": "crypto"},
    {"url": "https://decrypt.co/feed",                          "name": "Decrypt",       "w": 0.9, "cat": "crypto"},
    {"url": "https://feeds.feedburner.com/forexfactory/news",   "name": "ForexFactory",  "w": 1.2, "cat": "eco"},
    {"url": "https://www.ft.com/rss/home/uk",                   "name": "FT",            "w": 1.3, "cat": "eco"},
    {"url": "https://feeds.skynews.com/feeds/rss/world.xml",    "name": "SkyNews",       "w": 1.1, "cat": "geo"},
]


# ================================================================================
# FETCHERS
# ================================================================================

async def fetch_rss(session: aiohttp.ClientSession) -> List[Dict]:
    headlines   = []
    seen_titles = set()

    async def _get(src):
        for headers in [H_BROWSER, H_BOT, H_CURL]:
            try:
                async with session.get(src["url"],
                                       timeout=aiohttp.ClientTimeout(total=9),
                                       headers=headers) as r:
                    if r.status == 200:
                        text = await r.text(errors='replace')
                        try:
                            root = ET.fromstring(text)
                        except ET.ParseError:
                            continue
                        for item in root.findall(".//item")[:12]:
                            t = (item.findtext("title") or "").strip()
                            d = (item.findtext("description") or "").strip()
                            if t and t not in seen_titles:
                                seen_titles.add(t)
                                headlines.append({
                                    "title":  t,
                                    "text":   (t + " " + d).lower(),
                                    "source": src["name"],
                                    "cat":    src.get("cat", ""),
                                    "w":      src["w"],
                                })
                        return
                    elif r.status in (301, 302, 307, 308):
                        continue
            except asyncio.TimeoutError:
                break
            except Exception:
                break

    await asyncio.gather(*[_get(s) for s in RSS_SOURCES], return_exceptions=True)
    logger.info("[RSS] %d titres uniques de %d sources", len(headlines), len(RSS_SOURCES))
    return headlines


async def fetch_macro_yahoo(session: aiohttp.ClientSession, interval: str = "5m") -> Dict:
    """
    [FIX-3] Données macro avec support 1h pour analyse session.
    interval = "5m" (scalp) ou "1h" (session direction)
    """
    range_map = {"5m": "1d", "1h": "5d", "1d": "1mo"}
    r_range   = range_map.get(interval, "1d")

    result = {"vix": 18.0, "dxy": 101.0, "us10y": 4.3, "sp500": 5200.0,
              "nasdaq": 18000.0, "gold": 3300.0, "silver": 33.0,
              "btc_yf": 95000.0, "eurusd": 1.085, "usdjpy": 149.0,
              "oil": 75.0, "audusd": 0.65,
              "ok": False, "source": "fallback"}

    tickers = {
        "vix":    "^VIX",
        "dxy":    "DX-Y.NYB",
        "us10y":  "^TNX",
        "sp500":  "^GSPC",
        "nasdaq": "^IXIC",
        "gold":   "GC=F",
        "silver": "SI=F",
        "btc_yf": "BTC-USD",
        "eurusd": "EURUSD=X",
        "usdjpy": "USDJPY=X",
        "oil":    "CL=F",       # [v4] WTI crude — corrélé NOK/SEK/MXN/AUS200
        "audusd": "AUDUSD=X",   # [v4] AUD/USD — corrélé AUS200
    }

    for k, ticker in tickers.items():
        fetched = False
        for base_url in ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"]:
            for headers in [H_BROWSER, H_BOT]:
                try:
                    url = f"{base_url}/v8/finance/chart/{ticker}?range={r_range}&interval={interval}"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=8),
                                           headers=headers) as r:
                        if r.status == 200:
                            d   = await r.json(content_type=None)
                            cls = (d.get("chart", {}).get("result", [{}])[0]
                                   .get("indicators", {}).get("quote", [{}])[0]
                                   .get("close", []))
                            vals = [x for x in cls if x is not None]
                            if vals:
                                result[k]        = round(float(vals[-1]), 3)
                                result["ok"]     = True
                                result["source"] = "yahoo"
                                fetched = True
                                break
                except Exception:
                    pass
                if fetched:
                    break
            if fetched:
                break

    return result


async def fetch_binance_prices(session: aiohttp.ClientSession) -> Dict:
    # [FIX-418] Binance banni depuis Render Frankfurt (HTTP 418)
    # Source 1: CryptoCompare (gratuit, pas de ban IP, pas de clé API requise)
    # Source 2: CoinGecko fallback
    # Source 3: Binance en dernier recours (sera probablement bloqué)
    result = {"btc": 95000.0, "eth": 3500.0, "sol": 150.0, "xrp": 0.60,
              "btc_chg24h": 0.0, "eth_chg24h": 0.0,
              "btc_vol24h_B": 0.0, "ok": False, "source": "fallback"}

    # Source 1: CryptoCompare — batch en 1 seul appel
    try:
        url = "https://min-api.cryptocompare.com/data/pricemultifull?fsyms=BTC,ETH,SOL,XRP&tsyms=USD"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=7),
                               headers=H_BROWSER) as r:
            if r.status == 200:
                d = await r.json(content_type=None)
                raw = d.get("RAW", {})
                def _cc(sym, field, default=0.0):
                    return float(raw.get(sym, {}).get("USD", {}).get(field, default))
                result["btc"]          = round(_cc("BTC", "PRICE", 95000), 2)
                result["eth"]          = round(_cc("ETH", "PRICE", 3500), 2)
                result["sol"]          = round(_cc("SOL", "PRICE", 150), 4)
                result["xrp"]          = round(_cc("XRP", "PRICE", 0.60), 4)
                result["btc_chg24h"]   = round(_cc("BTC", "CHANGEPCT24HOUR"), 3)
                result["eth_chg24h"]   = round(_cc("ETH", "CHANGEPCT24HOUR"), 3)
                result["btc_vol24h_B"] = round(_cc("BTC", "TOTALVOLUME24HTO") / 1e9, 2)
                result["ok"]           = True
                result["source"]       = "cryptocompare"
    except Exception:
        pass

    # Source 2: CoinGecko fallback si CryptoCompare KO
    if not result["ok"]:
        try:
            url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana,ripple&vs_currencies=usd&include_24hr_change=true&include_24hr_vol=true"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8),
                                   headers=H_BOT) as r:
                if r.status == 200:
                    d = await r.json(content_type=None)
                    result["btc"]        = float(d.get("bitcoin", {}).get("usd", 95000))
                    result["btc_chg24h"] = float(d.get("bitcoin", {}).get("usd_24h_change", 0))
                    result["eth"]        = float(d.get("ethereum", {}).get("usd", 3500))
                    result["eth_chg24h"] = float(d.get("ethereum", {}).get("usd_24h_change", 0))
                    result["ok"]         = True
                    result["source"]     = "coingecko_fallback"
        except Exception:
            pass

    # Source 3: Binance en dernier recours (souvent bloqué depuis Render Frankfurt)
    if not result["ok"]:
        symbols = {"BTCUSDT": "btc", "ETHUSDT": "eth"}
        for sym_b, key in symbols.items():
            try:
                url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={sym_b}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5),
                                       headers=H_CURL) as r:
                    if r.status == 200:
                        d = await r.json(content_type=None)
                        result[key]             = round(float(d["lastPrice"]), 4)
                        result[f"{key}_chg24h"] = round(float(d["priceChangePercent"]), 3)
                        if key == "btc":
                            result["btc_vol24h_B"] = round(float(d["quoteVolume"]) / 1e9, 2)
                        result["ok"]     = True
                        result["source"] = "binance_fallback"
            except Exception:
                pass

    return result


async def fetch_fear_greed(session: aiohttp.ClientSession) -> Dict:
    result = {"value": 50, "label": "Neutral", "delta_1d": 0, "delta_3d": 0,
              "trend": "STABLE", "ok": False}
    for headers in [H_BOT, H_BROWSER, H_CURL]:
        try:
            async with session.get("https://api.alternative.me/fng/?limit=4&format=json",
                                   timeout=aiohttp.ClientTimeout(total=7),
                                   headers=headers) as r:
                if r.status == 200:
                    data = json.loads(await r.text()).get("data", [])
                    if data:
                        val   = int(data[0]["value"])
                        prev1 = int(data[1]["value"]) if len(data) > 1 else val
                        prev3 = int(data[3]["value"]) if len(data) > 3 else val
                        d1, d3 = val - prev1, val - prev3
                        result = {
                            "value":  val,
                            "label":  data[0]["value_classification"],
                            "delta_1d": d1, "delta_3d": d3,
                            "trend": "IMPROVING" if d3 > 5 else ("WORSENING" if d3 < -5 else "STABLE"),
                            "ok": True,
                        }
                        return result
        except Exception:
            pass
    return result


async def fetch_coingecko_global(session: aiohttp.ClientSession) -> Dict:
    result = {"btc_dominance": 50.0, "eth_dominance": 15.0, "market_chg_24h": 0.0,
              "altcoin_season": False, "total_mcap_T": 0.0, "ok": False}
    for headers in [H_BOT, H_BROWSER]:
        try:
            async with session.get("https://api.coingecko.com/api/v3/global",
                                   timeout=aiohttp.ClientTimeout(total=8),
                                   headers=headers) as r:
                if r.status == 200:
                    dat  = (await r.json(content_type=None)).get("data", {})
                    dom  = dat.get("market_cap_percentage", {})
                    btc_d = float(dom.get("btc", 50))
                    mcap  = dat.get("total_market_cap", {}).get("usd", 0)
                    result = {
                        "btc_dominance":  round(btc_d, 1),
                        "eth_dominance":  round(float(dom.get("eth", 15)), 1),
                        "market_chg_24h": round(float(dat.get("market_cap_change_percentage_24h_usd", 0)), 2),
                        "altcoin_season": btc_d < 45.0,
                        "total_mcap_T":   round(float(mcap) / 1e12, 2) if mcap else 0.0,
                        "ok": True,
                    }
                    return result
        except Exception:
            pass
    return result


async def fetch_etf_flows(session: aiohttp.ClientSession, sym: str, etfs: List[str]) -> Dict:
    """
    [FIX-6] Volume ETF directionnel : prix montant + volume élevé = vrai INFLOW
    Avant : volume seul → ambiguïté (liquidation = volume élevé MAIS bearish)
    Après : direction confirmée par comparaison close[-1] vs close[-2]
    """
    if not etfs:
        return {"signal": "NEUTRAL", "ok": False, "details": []}

    all_results = []
    for etf in etfs[:2]:
        for headers in [H_BROWSER, H_BOT]:
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{etf}?range=5d&interval=1h"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=9),
                                       headers=headers) as r:
                    if r.status == 200:
                        d    = await r.json(content_type=None)
                        res0 = d["chart"]["result"][0]
                        q    = res0.get("indicators", {}).get("quote", [{}])[0]
                        vols = [v for v in q.get("volume", []) if v and v > 0]
                        cls  = [c for c in q.get("close", [])  if c and c > 0]

                        if len(vols) >= 5 and len(cls) >= 2:
                            avg   = sum(vols[-11:-1]) / min(10, max(len(vols) - 1, 1))
                            last  = vols[-1]
                            ratio = last / avg if avg > 0 else 1.0
                            price = cls[-1]

                            # [FIX-6] Direction réelle du volume
                            price_direction = "UP"   if cls[-1] > cls[-2] else \
                                              "DOWN" if cls[-1] < cls[-2] else "FLAT"
                            trend3 = sum(vols[-4:-1]) / (3 * avg) if avg > 0 else 1.0

                            # Signal combiné : ratio volume + direction prix
                            if ratio > 1.30 and price_direction == "UP":
                                sig = "STRONG_INFLOW"    # Volume fort + prix monte → achats
                            elif ratio > 1.10 and price_direction == "UP":
                                sig = "INFLOW"
                            elif ratio > 1.30 and price_direction == "DOWN":
                                sig = "STRONG_OUTFLOW"   # Volume fort + prix baisse → liquidation
                            elif ratio > 1.10 and price_direction == "DOWN":
                                sig = "OUTFLOW"
                            elif ratio < 0.70:
                                sig = "LOW_VOLUME"       # Volume faible → indécision
                            else:
                                sig = "NEUTRAL"

                            all_results.append({
                                "etf":            etf,
                                "signal":         sig,
                                "ratio":          round(ratio, 2),
                                "trend_3d":       round(trend3, 2),
                                "price_dir":      price_direction,
                                "flow_M$":        round(last * price / 1e6, 1),
                                "last_vol":       int(last),
                                "avg_vol":        int(avg),
                            })
                        break
            except Exception:
                pass

    if not all_results:
        return {"signal": "NEUTRAL", "ok": False, "details": []}

    bull   = sum(1 for r in all_results if r["signal"] in ("INFLOW", "STRONG_INFLOW"))
    bear   = sum(1 for r in all_results if r["signal"] in ("OUTFLOW", "STRONG_OUTFLOW"))
    strong = any(r["signal"] in ("STRONG_INFLOW", "STRONG_OUTFLOW") for r in all_results)

    if bull > bear:    consolidated = "STRONG_INFLOW"  if strong else "INFLOW"
    elif bear > bull:  consolidated = "STRONG_OUTFLOW" if strong else "OUTFLOW"
    else:              consolidated = "NEUTRAL"

    return {
        "signal":        consolidated,
        "details":       all_results,
        "total_flow_M$": round(sum(r.get("flow_M$", 0) for r in all_results), 1),
        "ok":            True,
    }


async def fetch_fred_rate(session: aiohttp.ClientSession) -> Dict:
    """Taux Fed Funds officiels via FRED (CSV gratuit sans clé)."""
    result = {"fed_rate": 4.50, "ok": False, "source": "fallback"}
    for headers in [H_BOT, H_BROWSER]:
        try:
            url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=FEDFUNDS"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10),
                                   headers=headers) as r:
                if r.status == 200:
                    text  = await r.text()
                    lines = [l for l in text.strip().split("\n")
                             if "," in l and not l.startswith("DATE")]
                    if lines:
                        last = lines[-1].split(",")
                        if len(last) >= 2 and last[1].strip() != ".":
                            result = {
                                "fed_rate": round(float(last[1].strip()), 2),
                                "fed_date": last[0].strip(),
                                "ok":       True,
                                "source":   "FRED",
                            }
                            return result
        except Exception:
            pass
    return result


async def fetch_fred_cpi(session: aiohttp.ClientSession) -> Dict:
    """
    [FIX-8] CPI mensuel via FRED (CPIAUCSL).
    Enrichit l'analyse macro : inflation élevée → hawkish → bearish crypto/risk
    """
    result = {"cpi": 3.0, "cpi_yoy": 3.0, "ok": False}
    for headers in [H_BOT, H_BROWSER]:
        try:
            url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10),
                                   headers=headers) as r:
                if r.status == 200:
                    text  = await r.text()
                    lines = [l.split(",") for l in text.strip().split("\n")
                             if "," in l and not l.startswith("DATE")]
                    if len(lines) >= 13:
                        last_val  = float(lines[-1][1].strip())
                        prev_val  = float(lines[-13][1].strip())  # 12 mois avant
                        yoy       = round((last_val - prev_val) / prev_val * 100, 2)
                        result    = {
                            "cpi":     round(last_val, 2),
                            "cpi_yoy": yoy,
                            "cpi_date": lines[-1][0].strip(),
                            "ok":      True,
                        }
                        return result
        except Exception:
            pass
    return result


async def fetch_fred_jolts(session: aiohttp.ClientSession) -> Dict:
    """
    [FIX-8 COMPLET] JOLTS Job Openings via FRED (JTSJOL).
    JOLTS élevé (>9M) → marché du travail tendu → Fed hawkish → bearish risk
    JOLTS faible (<7M) → refroidissement → dovish probable → bullish risk
    """
    result = {"jolts": 8.0, "jolts_mom": 0.0, "ok": False}
    for headers in [H_BOT, H_BROWSER]:
        try:
            url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=JTSJOL"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10),
                                   headers=headers) as r:
                if r.status == 200:
                    text  = await r.text()
                    lines = [l.split(",") for l in text.strip().split("\n")
                             if "," in l and not l.startswith("DATE")]
                    if len(lines) >= 2:
                        last_val  = float(lines[-1][1].strip())
                        prev_val  = float(lines[-2][1].strip())
                        mom       = round(last_val - prev_val, 2)
                        result    = {
                            "jolts":      round(last_val / 1000, 2),  # en millions
                            "jolts_mom":  round(mom / 1000, 2),
                            "jolts_date": lines[-1][0].strip(),
                            "ok":         True,
                        }
                        return result
        except Exception:
            pass
    return result


async def fetch_nfp_bls(session: aiohttp.ClientSession) -> Dict:
    """
    [FIX-8 COMPLET] NFP (Non-Farm Payrolls) via FRED série PAYEMS.
    NFP > 200k → économie forte → Fed hawkish probable → bearish crypto/or
    NFP < 100k → refroidissement → dovish → bullish crypto/or
    Source FRED PAYEMS = équivalent BLS sans clé API.
    """
    result = {"nfp": 150.0, "nfp_mom": 0.0, "ok": False}
    for headers in [H_BOT, H_BROWSER]:
        try:
            url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=PAYEMS"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10),
                                   headers=headers) as r:
                if r.status == 200:
                    text  = await r.text()
                    lines = [l.split(",") for l in text.strip().split("\n")
                             if "," in l and not l.startswith("DATE")]
                    if len(lines) >= 2:
                        last_val  = float(lines[-1][1].strip())
                        prev_val  = float(lines[-2][1].strip())
                        mom_k     = round((last_val - prev_val), 1)  # en milliers
                        result    = {
                            "nfp":      round(last_val, 0),
                            "nfp_mom":  mom_k,       # variation mensuelle (000s)
                            "nfp_date": lines[-1][0].strip(),
                            "ok":       True,
                        }
                        return result
        except Exception:
            pass
    return result


KW_BEIGE_BOOK = [
    "beige book", "fed district", "economic conditions",
    "federal reserve report", "regional economy", "anecdotal evidence",
    "district report", "economic activity expanded", "economic activity contracted",
    "labor market tight", "wage growth", "price pressures",
]

def detect_beige_book_in_headlines(headlines: List[Dict]) -> Dict:
    """
    [FIX-8 COMPLET] Détection Beige Book dans les RSS.
    Le Beige Book est publié 8x/an, 2 semaines avant chaque FOMC.
    Ton hawkish → bearish risk / ton dovish → bullish risk.
    """
    result = {"detected": False, "tone": "neutral", "score": 0.0, "count": 0}
    if not headlines:
        return result

    hawkish_kw = ["tight labor", "wage pressure", "price pressure", "inflation persistent",
                  "demand strong", "capacity constraint", "supply constraint"]
    dovish_kw  = ["economic slowdown", "activity slowed", "demand weakened", "layoffs",
                  "hiring paused", "price eased", "inflation moderated"]

    bb_hits  = 0
    hawk_sc  = 0
    dove_sc  = 0

    for h in headlines[:80]:
        text = h["text"]
        if any(kw in text for kw in KW_BEIGE_BOOK):
            bb_hits += 1
            hawk_sc += sum(1 for kw in hawkish_kw if kw in text)
            dove_sc += sum(1 for kw in dovish_kw  if kw in text)

    if bb_hits >= 1:
        if hawk_sc > dove_sc:
            tone  = "hawkish"
            score = -0.20
        elif dove_sc > hawk_sc:
            tone  = "dovish"
            score = +0.15
        else:
            tone  = "neutral"
            score = 0.0
        result = {"detected": True, "tone": tone, "score": score,
                  "count": bb_hits, "hawk": hawk_sc, "dove": dove_sc}

    return result


# ================================================================================
# [FIX-11] MACRO SNAPSHOT — Champs requis par le serveur V16
# ================================================================================

def compute_macro_snapshot(macro: Dict, binance: Dict, cg: Dict, fred: Dict) -> Dict:
    """
    [FIX-11] Calcule risk_off_composite, nasdaq_risk_on, dxy_momentum, tnx_momentum.

    Ces 4 champs sont CRITIQUES pour le serveur V16 :
    • risk_off_composite → AI-32 ajuste les lots (×0.25 si >0.80)
    • nasdaq_risk_on     → Direction Fusion Engine v2, cross_asset
    • dxy_momentum       → compute_cross_asset bloque SELL XAU/crypto si DXY explosif
    • tnx_momentum       → Idem pour hausse taux TNX → bearish or

    Logique identique à fetch_macro_snapshot() du serveur V16 (lignes 4030-4103).
    """
    vix     = float(macro.get("vix",    18.0) or 18.0)
    dxy     = float(macro.get("dxy",   101.0) or 101.0)
    us10y   = float(macro.get("us10y",   4.3) or 4.3)
    sp500   = float(macro.get("sp500", 5200.0) or 5200.0)
    nasdaq  = float(macro.get("nasdaq",18000.0) or 18000.0)
    gold    = float(macro.get("gold",  3300.0) or 3300.0)
    silver  = float(macro.get("silver",  33.0) or 33.0)

    btc_chg = float(binance.get("btc_chg24h", 0.0) or 0.0)

    # ── VIX normalisé → composante risk-off (0=risque faible, 1=panique)
    vix_norm = float(min(1.0, max(0.0, (vix - 12.0) / 28.0)))   # 12→0.0, 40→1.0

    # ── NASDAQ risk-on signal : chg 24h normalisé (±2% → ±1.0)
    # Proxy via SP500 si nasdaq non disponible comme série de chg
    nasdaq_chg = float(macro.get("nasdaq_chg24h", 0.0) or 0.0)
    if nasdaq_chg == 0.0 and sp500 > 0:
        # Pas de chg direct : approximation via momentum (SP500 vs EMA implicite)
        # On utilise btc_chg comme proxy risk-on partiel
        nasdaq_chg = btc_chg * 0.3   # corrélation partielle BTC/NASDAQ

    nasdaq_risk_on = float(min(1.0, max(-1.0, nasdaq_chg / 2.0)))

    # ── DXY momentum : normalise la valeur brute vs pivot 101.0
    # DXY > 104 = dollar très fort → risk-off composite monte
    dxy_deviation = (dxy - 101.0) / 10.0    # 101→0.0, 111→+1.0, 91→-1.0
    dxy_norm      = float(min(1.0, max(-1.0, dxy_deviation)))

    # ── US10Y momentum normalisé
    us10y_n = float(min(1.0, max(-1.0, (us10y - 4.0) / 3.0)))   # 4.0→0, 7.0→+1.0

    # ── risk_off_composite : 0=risk-on total, 1=risk-off total
    # Formule alignée avec le serveur V16 (lignes 4050-4056)
    risk_off_composite = float(min(1.0, max(0.0,
        0.35 * vix_norm
        + 0.25 * (-nasdaq_risk_on * 0.5 + 0.5)   # NASDAQ inversé → risk-off quand NASDAQ baisse
        + 0.25 * max(0.0, dxy_norm)               # DXY fort → risk-off
        + 0.15 * max(0.0, us10y_n)                # Taux élevés → risk-off
    )))

    # ── DXY momentum (pour cross_asset : >0.5 → SELL forcé XAU/crypto)
    # Mesure l'accélération du DXY sur 24h (approximation depuis valeur absolue)
    dxy_momentum = float(min(1.0, max(-1.0, dxy_norm * 1.5)))   # amplifié pour trigger cross_asset

    # ── TNX momentum (pour cross_asset : >0.4 → bearish or)
    tnx_momentum = float(min(1.0, max(-1.0, us10y_n * 1.2)))

    # ── Régime global
    if risk_off_composite > 0.65:
        risk_regime = "RISK_OFF"
    elif risk_off_composite < 0.30:
        risk_regime = "RISK_ON"
    else:
        risk_regime = "NEUTRAL"

    return {
        # Champs bruts
        "vix":              vix,
        "dxy":              dxy,
        "us10y":            us10y,
        "sp500":            sp500,
        "nasdaq":           nasdaq,
        "gold":             gold,
        "silver":           silver,
        "btc_chg24h":       btc_chg,
        # [FIX-11] Champs dérivés requis par V16
        "vix_norm":              round(vix_norm, 3),
        "nasdaq_risk_on":        round(nasdaq_risk_on, 3),   # +1=risk-on, -1=risk-off
        "dxy_norm":              round(dxy_norm, 3),
        "us10y_norm":            round(us10y_n, 3),
        "risk_off_composite":    round(risk_off_composite, 3),  # 0=risk-on, 1=risk-off total
        "dxy_momentum":          round(dxy_momentum, 3),        # pour cross_asset
        "tnx_momentum":          round(tnx_momentum, 3),        # pour cross_asset XAU
        "risk_regime":           risk_regime,                   # "RISK_OFF"/"RISK_ON"/"NEUTRAL"
        "btc_dom":               float(cg.get("btc_dominance", 50) or 50),
        "fed_rate":              float(fred.get("fed_rate", 4.5) or 4.5),
    }


# ================================================================================
# SCORING — POIDS ADAPTATIFS PAR ACTIF ET PAR RÉGIME VIX [FIX-7]
# ================================================================================

def compute_weights(vix: float, asset_type: str = "forex", urgency: bool = False) -> Dict[str, float]:
    """
    [FIX-7] Poids adaptatifs PAR ACTIF + par régime VIX + session intégrée.

    Crypto  → tech 25% (momentum dominant en crypto)
    Metal   → news 35% (or réagit aux news géo avant tout)
    Forex   → macro 30% (taux, DXY, macroéconomie dominent)
    Index   → macro+etf 55% (indices = consensus institutionnel)
    """
    base_by_type = {
        "crypto": {"news": 0.25, "etf": 0.20, "fg": 0.15, "macro": 0.15, "tech": 0.15, "session": 0.10},
        "metal":  {"news": 0.35, "etf": 0.18, "fg": 0.10, "macro": 0.20, "tech": 0.10, "session": 0.07},
        "forex":  {"news": 0.20, "etf": 0.15, "fg": 0.08, "macro": 0.30, "tech": 0.20, "session": 0.07},
        "index":  {"news": 0.20, "etf": 0.28, "fg": 0.10, "macro": 0.27, "tech": 0.10, "session": 0.05},
    }
    w = dict(base_by_type.get(asset_type, base_by_type["forex"]))

    if urgency or vix > 30:
        boost  = {"news": 0.08, "etf": 0.07}
        reduce = {"tech": -0.08, "session": -0.05, "fg": -0.02}
        for k, v in {**boost, **reduce}.items():
            if k in w:
                w[k] = max(0.0, w[k] + v)
    elif vix > VIX_STRESS:
        boost  = {"news": 0.04, "etf": 0.03}
        reduce = {"tech": -0.04, "session": -0.03}
        for k, v in {**boost, **reduce}.items():
            if k in w:
                w[k] = max(0.0, w[k] + v)

    total = sum(w.values())
    return {k: round(v / total, 4) for k, v in w.items()}


def score_news(headlines: List[Dict], profile: Dict, geo_pol_dynamic: str = None) -> Tuple[float, List[str]]:
    """
    [FIX-2] Score news corrigé — séparation claire bull/bear par type d'actif.
    geo_pol_dynamic : valeur calculée en temps réel (remplace geo_pol_base statique)
    """
    geo_pol = geo_pol_dynamic or profile.get("geo_pol_base", "neutral")
    typ     = profile.get("type", "forex")

    if typ == "metal":
        bull_kw = KW_METAL_BULL
        bear_kw = KW_METAL_BEAR
    elif typ in ("crypto", "index"):
        bull_kw = KW_BULL_RISK
        bear_kw = KW_BEAR_RISK
    else:  # forex
        bull_kw = KW_BULL_RISK
        bear_kw = KW_BEAR_RISK

    bull_s = bear_s = 0.0
    triggered = []

    for h in headlines[:60]:
        w    = h["w"]
        text = h["text"]
        hb   = sum(1 for kw in bull_kw if kw in text)
        hbr  = sum(1 for kw in bear_kw if kw in text)

        if hb or hbr:
            triggered.append(f"[{h['source']}] {h['title'][:80]}")

        # [FIX-2] Logique claire : bull_kw → score positif, bear_kw → score négatif
        bull_s += hb  * w * 0.8
        bear_s += hbr * w * 0.8

    total = (bull_s + bear_s) or 1.0
    score = round(max(-1.0, min(1.0, (bull_s - bear_s) / total)), 3)

    # Ajustement géopolitique dynamique
    if geo_pol == "bull" and typ == "metal":
        if bear_s > bull_s:
            score = abs(score) * 0.9  # Guerre = BUY or
    elif geo_pol == "bull" and typ in ("crypto", "index"):
        score = min(1.0, score + 0.10)
    elif geo_pol == "bear" and typ in ("crypto", "index"):
        score = max(-1.0, score - 0.10)

    return round(max(-1.0, min(1.0, score)), 3), triggered[:6]


def score_etf(etf_data: Dict) -> float:
    """Score ETF flows : -1.0 à +1.0."""
    m = {
        "STRONG_INFLOW":  +0.90,
        "INFLOW":         +0.50,
        "NEUTRAL":         0.00,
        "LOW_VOLUME":     -0.10,
        "OUTFLOW":        -0.50,
        "STRONG_OUTFLOW": -0.90,
    }
    base = m.get(etf_data.get("signal", "NEUTRAL"), 0.0)

    details = etf_data.get("details", [])
    if details:
        trend3 = details[0].get("trend_3d", 1.0)
        if trend3 < 0.80 and base < 0:
            base = max(-1.0, base * 1.15)
        elif trend3 > 1.20 and base > 0:
            base = min(1.0,  base * 1.15)

    return round(base, 3)


def score_fear_greed(fg: Dict, profile: Dict) -> float:
    """Score F&G : inverse pour métaux, direct pour le reste."""
    val  = fg.get("value", 50)
    d3   = fg.get("delta_3d", 0)
    typ  = profile.get("type", "forex")

    base = -(val - 50) / 50.0 if typ == "metal" else (val - 50) / 50.0

    if abs(d3) > 8:
        up = d3 > 0
        if (up and typ != "metal") or (not up and typ == "metal"):
            base = max(-1.0, min(1.0, base + 0.22))
        else:
            base = max(-1.0, min(1.0, base - 0.22))

    return round(base, 3)


def score_macro(macro: Dict, binance: Dict, cg: Dict, fred: Dict,
                fred_cpi: Dict, profile: Dict,
                fred_jolts: Dict = None, fred_nfp: Dict = None,
                beige_book: Dict = None) -> float:
    """
    [FIX-8 COMPLET] Score macro enrichi :
      - CPI via FRED (CPIAUCSL)
      - NFP via FRED PAYEMS (variation mensuelle emplois)
      - JOLTS via FRED JTSJOL (offres d'emploi)
      - Beige Book détecté dans RSS (ton hawkish/dovish)
    [ANOMALIE CORRIGÉE] Score forex étoffé : taux Fed, CPI, NFP, JOLTS intégrés
    """
    if fred_jolts  is None: fred_jolts  = {}
    if fred_nfp    is None: fred_nfp    = {}
    if beige_book  is None: beige_book  = {}

    typ    = profile.get("type", "forex")
    sym    = profile.get("sym", "")
    vix    = float(macro.get("vix",    18) or 18)
    dxy    = float(macro.get("dxy",   101) or 101)
    u10    = float(macro.get("us10y", 4.3) or 4.3)
    spx    = float(macro.get("sp500",5200) or 5200)
    nasdaq = float(macro.get("nasdaq",18000) or 18000)

    btc      = float(binance.get("btc",  95000) or 95000)
    btc_chg  = float(binance.get("btc_chg24h", 0) or 0)
    btc_dom  = float(cg.get("btc_dominance", 50) or 50)
    mkt_chg  = float(cg.get("market_chg_24h", 0) or 0)
    fed_rate = float(fred.get("fed_rate", 4.5) or 4.5)

    cpi_yoy  = float(fred_cpi.get("cpi_yoy",  3.0) or 3.0)
    jolts_m  = float(fred_jolts.get("jolts",   8.0) or 8.0)   # millions d'offres
    nfp_mom  = float(fred_nfp.get("nfp_mom",   150) or 150)   # variation k emplois/mois
    bb_score = float(beige_book.get("score",   0.0) or 0.0)   # -0.20 hawk / +0.15 dove

    # ─── Calcul score hawkish/dovish composite (utilisé dans tous les types) ──
    # NFP > 200k = économie forte = hawkish = -score (bearish risk/or)
    # NFP < 100k = refroidissement = dovish = +score (bullish risk/or)
    nfp_bias = 0.0
    if fred_nfp.get("ok"):
        if nfp_mom > 300:   nfp_bias = -0.25
        elif nfp_mom > 200: nfp_bias = -0.15
        elif nfp_mom > 100: nfp_bias = -0.05
        elif nfp_mom < 0:   nfp_bias = +0.20
        elif nfp_mom < 50:  nfp_bias = +0.12

    # JOLTS : offres d'emploi
    jolts_bias = 0.0
    if fred_jolts.get("ok"):
        if jolts_m > 9.5:   jolts_bias = -0.15  # marché du travail surchauffé
        elif jolts_m > 8.5: jolts_bias = -0.08
        elif jolts_m < 6.5: jolts_bias = +0.15  # refroidissement
        elif jolts_m < 7.5: jolts_bias = +0.07

    # Beige Book : ajout direct du score calculé depuis RSS
    bb_bias = bb_score  # déjà normalisé entre -0.20 et +0.15

    if typ == "metal":
        s = 0.0
        if dxy > 105: s -= 0.50
        elif dxy > 103: s -= 0.25
        elif dxy < 98: s += 0.50
        elif dxy < 100: s += 0.25

        if vix > 30: s += 0.45
        elif vix > 25: s += 0.25
        elif vix > 22: s += 0.12
        elif vix < 12: s -= 0.15

        if u10 > 5.0: s -= 0.40
        elif u10 > 4.7: s -= 0.20
        elif u10 < 3.5: s += 0.30
        elif u10 < 4.0: s += 0.12

        # CPI : inflation élevée → bullish or (hedge inflation)
        if cpi_yoy > 5.0: s += 0.30
        elif cpi_yoy > 3.5: s += 0.15
        elif cpi_yoy < 2.0: s -= 0.10

        if fed_rate > 5.0: s -= 0.18
        elif fed_rate < 3.5: s += 0.18

        # NFP/JOLTS : marché fort = Fed hawkish = taux réels montants = bearish or
        s -= nfp_bias * 0.8    # inverse : emploi fort = bearish or
        s -= jolts_bias * 0.7
        s += bb_bias * 0.6     # Beige Book dovish = bullish or

        silver_price = float(macro.get("silver", 0) or 0)
        gold_price   = float(macro.get("gold",   3300) or 3300)
        if silver_price > 0 and gold_price > 0:
            gsr = gold_price / silver_price
            if gsr > 90: s += 0.15
            elif gsr > 85: s += 0.08
            elif gsr < 70: s -= 0.10

        if "XAG" in sym:
            if spx > 5400: s += 0.08
            elif spx < 4800: s -= 0.10

    elif typ == "crypto":
        s = 0.0
        if vix > 32: s -= 0.60
        elif vix > 28: s -= 0.35
        elif vix > 23: s -= 0.15
        elif vix < 14: s += 0.20

        if spx > 5500: s += 0.25
        elif spx > 5200: s += 0.12
        elif spx < 4800: s -= 0.25
        elif spx < 4500: s -= 0.40

        if "BTCUSD" not in sym:
            if btc_chg > 3: s += 0.15
            elif btc_chg < -3: s -= 0.20

        if u10 > 5.0: s -= 0.30
        elif u10 > 4.7: s -= 0.15
        elif u10 < 3.5: s += 0.20

        if dxy > 105: s -= 0.25
        elif dxy < 98: s += 0.20

        if mkt_chg > 5: s += 0.10
        elif mkt_chg < -5: s -= 0.15

        # CPI : inflation trop haute → Fed hawkish → bearish crypto
        if cpi_yoy > 4.5: s -= 0.20
        elif cpi_yoy > 3.5: s -= 0.10
        elif cpi_yoy < 2.0: s += 0.10

        # NFP/JOLTS/Beige Book : emploi fort = Fed hawkish = bearish crypto
        s += nfp_bias    # même sens : emploi fort = bearish crypto
        s += jolts_bias
        s += bb_bias

    elif typ == "index":
        s = 0.0
        if vix > 30: s -= 0.45
        elif vix > 24: s -= 0.20
        elif vix < 14: s += 0.22
        if spx > 5400: s += 0.20
        elif spx < 4800: s -= 0.30
        if u10 > 5.0: s -= 0.25
        if fed_rate > 5.5: s -= 0.15
        if cpi_yoy > 4.0: s -= 0.15

        # NFP fort = bons earnings potentiels MAIS Fed hawkish = ambigu → poids réduit
        if nfp_mom > 200:  s += 0.05   # earnings optimism léger
        elif nfp_mom < 50: s -= 0.10   # peur récession
        s += jolts_bias * 0.4
        s += bb_bias * 0.5

    else:  # forex — ANOMALIE CORRIGÉE : section étoffée
        s = 0.0

        # VIX : aversion au risque → fuite vers USD/JPY/CHF
        if vix > 30: s -= 0.30
        elif vix > 27: s -= 0.20
        elif vix > 22: s -= 0.10
        elif vix < 14: s += 0.10

        # DXY : force du dollar US → impact sur paires
        if "EUR" in sym or "GBP" in sym or "AUD" in sym or "NZD" in sym:
            # USD est la devise cotée → DXY fort = baisse de ces paires
            if dxy > 105: s -= 0.25
            elif dxy > 103: s -= 0.15
            elif dxy < 99:  s += 0.20
            elif dxy < 101: s += 0.10
        elif "USDJPY" in sym or "USDCHF" in sym or "USDCAD" in sym:
            # USD est la devise de base → DXY fort = hausse
            if dxy > 105: s += 0.25
            elif dxy > 103: s += 0.15
            elif dxy < 99:  s -= 0.20
            elif dxy < 101: s -= 0.10

        # Taux US10Y : différentiel de taux → flux capitaux
        if "EUR" in sym or "GBP" in sym:
            if u10 > 5.0: s -= 0.20   # rendement US supérieur → USD attractif
            elif u10 < 3.5: s += 0.15
        elif "JPY" in sym:
            # JPY : différentiel BoJ vs Fed. Hausse US10Y = USDJPY monte
            if "USDJPY" in sym or "GBPJPY" in sym or "EURJPY" in sym:
                if u10 > 5.0: s += 0.20
                elif u10 > 4.5: s += 0.10
                elif u10 < 3.5: s -= 0.18

        # Taux Fed : USD fort si Fed hawkish
        if "USD" in sym:
            if "USDJPY" in sym or "USDCHF" in sym or "USDCAD" in sym:
                if fed_rate > 5.25: s += 0.12
                elif fed_rate < 3.5: s -= 0.12
            else:  # paires où USD est coté (EURUSD etc.)
                if fed_rate > 5.25: s -= 0.12
                elif fed_rate < 3.5: s += 0.12

        # CPI US élevé → Fed hawkish → USD fort
        if "EURUSD" in sym or "GBPUSD" in sym or "AUDUSD" in sym or "NZDUSD" in sym:
            if cpi_yoy > 4.5: s -= 0.15
            elif cpi_yoy > 3.5: s -= 0.08
            elif cpi_yoy < 2.0: s += 0.10
        elif "USDJPY" in sym or "USDCHF" in sym or "USDCAD" in sym:
            if cpi_yoy > 4.5: s += 0.15
            elif cpi_yoy > 3.5: s += 0.08
            elif cpi_yoy < 2.0: s -= 0.10

        # NFP : emploi fort US → USD fort
        if fred_nfp.get("ok"):
            usd_as_base = ("USDJPY" in sym or "USDCHF" in sym or "USDCAD" in sym)
            if usd_as_base:
                s -= nfp_bias   # NFP fort → USD up → USDJPY monte (nfp_bias négatif)
            else:
                s += nfp_bias   # NFP fort → EURUSD baisse (nfp_bias négatif → s baisse)

        # JOLTS : même logique que NFP
        if fred_jolts.get("ok"):
            usd_as_base = ("USDJPY" in sym or "USDCHF" in sym or "USDCAD" in sym)
            if usd_as_base:
                s -= jolts_bias
            else:
                s += jolts_bias

        # Beige Book hawkish → USD fort
        if "EURUSD" in sym or "GBPUSD" in sym or "AUDUSD" in sym:
            s -= bb_bias   # BB dovish (bb_bias > 0) → USD faible → EURUSD monte
        elif "USDJPY" in sym or "USDCHF" in sym:
            s += bb_bias   # BB dovish → USD faible → USDJPY baisse → s monte (inverse)

        # Paires refuge en crise
        if "JPY" in sym and vix > 25:
            if "USDJPY" in sym:
                s -= 0.15   # JPY refuge → USDJPY baisse
            else:
                s += 0.10   # JPY dans cross = cross monte car JPY refuge = complexe

        # CHF refuge
        if "CHF" in sym and vix > 25:
            if "USDCHF" in sym:
                s -= 0.12   # CHF fort → USDCHF baisse
            elif "EURCHF" in sym:
                s -= 0.10

        # ── [v4] Scoring actifs propres exotiques ─────────────────────────
        # Corrélation pétrole pour NOK/SEK/MXN
        oil = float(macro.get("oil", 75.0) or 75.0)
        if "NOK" in sym or "SEK" in sym:
            # USD/NOK et USD/SEK : pétrole haut = couronne forte = paire baisse
            if oil > 90: s -= 0.20
            elif oil > 80: s -= 0.10
            elif oil < 60: s += 0.15
            elif oil < 70: s += 0.08

        if "MXN" in sym:
            # USD/MXN : pétrole haut → MXN fort → paire baisse
            if oil > 90: s -= 0.18
            elif oil > 80: s -= 0.08
            elif oil < 60: s += 0.12
            # Risk-off US → USDMXN monte (MXN = monnaie à risque)
            if vix > 25: s += 0.20
            elif vix > 20: s += 0.10
            elif vix < 14: s -= 0.12

        if "CNH" in sym:
            # USD/CNH : macro US vs Chine
            # DXY fort → CNH faible → USDCNH monte
            if dxy > 105: s += 0.18
            elif dxy < 98: s -= 0.15
            # Tensions trade war → CNH faible → USDCNH monte
            if vix > 28: s += 0.12

        if "AUDNZD" in sym:
            # AUD/NZD : corrélé iron ore vs lait/agriculture NZ
            # Range bound → pas de biais macro fort → uniquement technique
            # Légère prime AUD si DXY faible (commodités up)
            if dxy < 99: s += 0.08
            elif dxy > 104: s -= 0.06

        if "SPAIN35" in sym:
            # IBEX35 : corrélé Euro, immobilier espagnol, tourisme
            if dxy < 99: s += 0.15   # EUR fort → IBEX up
            elif dxy > 104: s -= 0.12
            if vix > 25: s -= 0.20
            elif vix < 15: s += 0.12

        if "AUS200" in sym:
            # ASX200 : corrélé Chine (export minerais), commodités
            if oil > 85: s += 0.10
            if vix > 25: s -= 0.18
            elif vix < 14: s += 0.12
            # AUD fort = ASX200 bien corrélé
            audusd = float(macro.get("audusd", 0.65) or 0.65)
            if audusd > 0.70: s += 0.08
            elif audusd < 0.62: s -= 0.08

        if "LTCUSD" in sym:
            # Litecoin : suit BTC avec lag — utilise btc_chg comme proxy
            btc_chg = float(binance.get("btc_chg24h", 0) or 0)
            if btc_chg > 5: s += 0.20
            elif btc_chg > 2: s += 0.10
            elif btc_chg < -5: s -= 0.20
            elif btc_chg < -2: s -= 0.10
            # Même macro que crypto normal
            if vix > 28: s -= 0.25
            elif vix < 14: s += 0.15

    return round(max(-1.0, min(1.0, s)), 3)


# ================================================================================
# [FIX-4] SCORECARD DIRECTIONNEL — Réponse lisible à "BUY ou SELL ?"
# ================================================================================

def build_direction_scorecard(
    sym: str,
    final_score: float,
    direction: str,
    news_sc: float, etf_sc: float, fg_sc: float,
    mac_sc: float,  tech_sc: float, session_sc: float,
    weights: Dict,
    fg: Dict, macro: Dict, binance: Dict,
    tech_data: Dict, geo_pol: str,
    session_name: str, vol_mult: float,
    ff_events: List[Dict],
    conviction: float,
) -> Dict:
    """
    [FIX-4] Génère une scorecard complète répondant à :
    "QUELLE EST LA DIRECTION DE {sym} ACTUELLEMENT SELON LES DONNÉES ÉCONOMIQUES ?
     EST-IL BUY OU SELL ?"

    Format conçu pour être lisible par l'EA et par un humain.
    """
    dir_emoji = {"BUY": "🟢", "SELL": "🔴", "NEUTRAL": "⬜", "CRITICAL": "⚠️", "RESPIRATION": "🔵"}
    emoji     = dir_emoji.get(direction, "⬜")

    pillars = []

    # 1. NEWS
    news_lbl = "BULL" if news_sc > 0.15 else ("BEAR" if news_sc < -0.15 else "NEUTRE")
    pillars.append({
        "pilier": "NEWS_GÉOPOLITIQUE",
        "score":  round(news_sc, 3),
        "poids":  round(weights.get("news", 0.25) * 100),
        "signal": news_lbl,
        "geo_pol_actif": geo_pol,
    })

    # 2. ETF INSTITUTIONNEL
    etf_lbl = "INFLOW" if etf_sc > 0.3 else ("OUTFLOW" if etf_sc < -0.3 else "NEUTRE")
    pillars.append({
        "pilier": "ETF_INSTITUTIONNEL",
        "score":  round(etf_sc, 3),
        "poids":  round(weights.get("etf", 0.20) * 100),
        "signal": etf_lbl,
    })

    # 3. SENTIMENT F&G
    fg_val  = fg.get("value", 50)
    fg_lbl  = fg.get("label", "Neutral")
    fg_tend = fg.get("trend", "STABLE")
    pillars.append({
        "pilier": "FEAR_GREED",
        "score":  round(fg_sc, 3),
        "poids":  round(weights.get("fg", 0.12) * 100),
        "signal": f"F&G={fg_val} ({fg_lbl}) Δ3j={fg.get('delta_3d',0):+d} → {fg_tend}",
    })

    # 4. MACRO
    vix_v  = macro.get("vix",   18)
    dxy_v  = macro.get("dxy",  101)
    u10_v  = macro.get("us10y", 4.3)
    btc_v  = binance.get("btc", 0)
    btc_c  = binance.get("btc_chg24h", 0)
    pillars.append({
        "pilier": "MACRO_ÉCONOMIQUE",
        "score":  round(mac_sc, 3),
        "poids":  round(weights.get("macro", 0.20) * 100),
        "signal": f"VIX={vix_v:.1f} DXY={dxy_v:.2f} US10Y={u10_v:.2f}% BTC={btc_v:,.0f}$ ({btc_c:+.1f}%)",
    })

    # 5. TECHNIQUE RSI multi-tf
    if tech_data.get("ok"):
        by_tf = tech_data.get("by_tf", {})
        tech_detail = " | ".join([f"RSI{tf}={d['rsi']:.0f}({d['signal']})"
                                  for tf, d in by_tf.items()])
    else:
        tech_detail = "Données indisponibles"
    pillars.append({
        "pilier": "TECHNIQUE_RSI_MTF",
        "score":  round(tech_sc, 3),
        "poids":  round(weights.get("tech", 0.15) * 100),
        "signal": tech_detail,
    })

    # 6. SESSION
    pillars.append({
        "pilier": "SESSION_HORAIRE",
        "score":  round(session_sc, 3),
        "poids":  round(weights.get("session", 0.08) * 100),
        "signal": f"{session_name} | vol×{vol_mult:.1f}",
    })

    # Événements à risque
    risk_events = []
    for ev in ff_events:
        if ev.get("impact") == "HIGH":
            risk_events.append(f"⚠️ HIGH IMPACT: {ev.get('currency','')} — {ev.get('title','')[:50]} dans {ev.get('minutes_until',0)}min")
        elif ev.get("impact") == "MEDIUM":
            risk_events.append(f"🟡 MEDIUM: {ev.get('currency','')} — {ev.get('title','')[:40]} dans {ev.get('minutes_until',0)}min")

    # [GDE] Calcul des niveaux d'entrée limite (Grid Dynamic Entry)
    _sym_up  = sym.upper()
    _atype   = ("crypto" if any(x in _sym_up for x in ["BTC","ETH","XRP","SOL"])
                else "metal" if any(x in _sym_up for x in ["XAU","XAG"])
                else "index" if any(x in _sym_up for x in ["US30","US100","US500"])
                else "forex")
    _atr_map = {"crypto": 150.0, "metal": 4.0, "forex": 0.0008, "index": 25.0}
    _atr_e   = round(_atr_map.get(_atype, 0.001) * max(1.0, float(vix_val or 18) / 18.0), 5)
    _tp      = float(tech_data.get("price", 0)) if tech_data.get("ok") else 0.0
    if direction == "SELL" and _tp > 0:
        _gl1 = round(_tp + _atr_e * 0.5, 5)
        _gl2 = round(_tp + _atr_e * 1.0, 5)
    elif direction == "BUY" and _tp > 0:
        _gl1 = round(_tp - _atr_e * 0.5, 5)
        _gl2 = round(_tp - _atr_e * 1.0, 5)
    else:
        _gl1 = _gl2 = 0.0

        return {
        "symbole":        sym,
        "direction":      direction,
        "emoji":          emoji,
        "verdict":        f"{emoji} {sym} → {direction} (score={final_score:+.3f} | conviction={conviction:.0%})",
        "score_final":    round(final_score, 4),
        "conviction":     round(conviction, 3),
        "piliers":        pillars,
        "risques_macro":  risk_events,
        "resume":         f"{sym}: {'ACHETER' if direction=='BUY' else ('VENDRE' if direction=='SELL' else 'ATTENDRE')} "
                          f"— Score {final_score:+.3f} — Session {session_name} — "
                          f"F&G {fg_val}/100 — VIX {vix_v:.1f}",
        # [GDE] Niveaux d'entrée limite pour Grid Dynamic Entry
        "gde_level1":   _gl1,
        "gde_level2":   _gl2,
        "gde_atr_est":  _atr_e,
    }


# ================================================================================
# MOTEUR DE DÉCISION PRINCIPAL
# ================================================================================

def compute_asset_state(
    sym: str, profile: Dict,
    headlines: List[Dict],
    etf_data: Dict,
    fg: Dict, macro: Dict, binance: Dict, cg: Dict, fred: Dict,
    fred_cpi: Dict,
    fred_jolts: Dict = None,
    fred_nfp: Dict = None,
    beige_book: Dict = None,
    geo_pol_dynamic: str = None,
    urgency: bool = False,
) -> AssetState:
    """Calcule l'état complet d'un actif avec tous les signaux corrigés."""
    if fred_jolts is None: fred_jolts = {}
    if fred_nfp   is None: fred_nfp   = {}
    if beige_book is None: beige_book = {}

    vix    = float(macro.get("vix", 18) or 18)
    regime = "CRITICAL" if vix > 30 else ("HIGH" if vix > VIX_STRESS else "NORMAL")

    profile_with_sym = {**profile, "sym": sym}
    typ = profile.get("type", "forex")

    hour_utc = datetime.now(timezone.utc).hour

    # [FIX-7] Poids adaptatifs par actif
    w = compute_weights(vix, typ, urgency)

    # [FIX-2] Score news avec geo_pol dynamique
    news_sc, triggers = score_news(headlines, profile, geo_pol_dynamic)

    etf_sc  = score_etf(etf_data)
    fg_sc   = score_fear_greed(fg, profile)
    mac_sc  = score_macro(macro, binance, cg, fred, fred_cpi, profile_with_sym,
                          fred_jolts=fred_jolts, fred_nfp=fred_nfp, beige_book=beige_book)

    # RSI multi-timeframe [FIX-5]
    tech_data = _raw_cache.get("technicals", {}).get(sym, {})
    tech_sc   = float(tech_data.get("score", 0.0)) if tech_data.get("ok") else 0.0

    # [FIX-3] Score session horaire + wr_buy pour camera_allowed
    session_sc, session_name, vol_mult, session_wr_buy = get_session_bias_score(sym, hour_utc)

    # Calendrier ForexFactory
    ff_events  = _raw_cache.get("ff_events", {}).get("data", [])
    ff_penalty = 0.0
    for ev in ff_events:
        min_until = ev.get("minutes_until", 999)
        if ev.get("impact") == "HIGH" and min_until < 60:
            ff_penalty = -0.20
            break
        elif ev.get("impact") == "HIGH" and min_until < 240:
            ff_penalty = -0.12
        elif ev.get("impact") == "MEDIUM" and min_until < 30:
            ff_penalty = -0.07

    # Score final pondéré — 6 sources
    final = round(max(-1.0, min(1.0,
        news_sc    * w.get("news",    0.25) +
        etf_sc     * w.get("etf",     0.20) +
        fg_sc      * w.get("fg",      0.12) +
        mac_sc     * w.get("macro",   0.20) +
        tech_sc    * w.get("tech",    0.15) +
        session_sc * w.get("session", 0.08) +
        ff_penalty
    )), 4)

    # Direction
    force_release = False
    if final <= SCORE_CRITICAL:
        direction     = "CRITICAL"
        force_release = True
        regime        = "CRITICAL"
    elif final >= SCORE_BUY:
        direction = "BUY"
    elif final <= SCORE_SELL:
        direction = "SELL"
    elif abs(final) <= SCORE_RESPIR:
        direction = "RESPIRATION"
    else:
        direction = "NEUTRAL"

    conviction = round(min(1.0, abs(final) * 1.8), 2)

    # Raisons lisibles
    reasons = []
    if force_release:
        reasons.append(f"⚠️ FORCE_RELEASE — score CRITIQUE={final:.3f}")
    if triggers:
        reasons.append(f"[NEWS/{triggers[0].split(']')[0][1:]}] {triggers[0].split('] ')[1][:70]}")
    if abs(news_sc) > 0.15:
        reasons.append(f"[NEWS] {'▲' if news_sc>0 else '▼'}{news_sc:+.2f} ({len(triggers)} titres) geo_pol={geo_pol_dynamic}")

    etf_sig = etf_data.get("signal", "?")
    if etf_data.get("ok") and etf_sig != "NEUTRAL":
        fl      = etf_data.get("total_flow_M$", 0)
        det     = etf_data.get("details", [])
        etf_nm  = det[0].get("etf", "?") if det else "?"
        price_d = det[0].get("price_dir", "?") if det else "?"
        reasons.append(f"[ETF/{etf_nm}] {etf_sig} {fl:+.0f}M$ (prix {price_d})")

    fg_v = fg.get("value", 50)
    reasons.append(f"[SENTIMENT] F&G={fg_v}/100 ({fg.get('label','?')}) "
                   f"Δ1j={fg.get('delta_1d',0):+d} Δ3j={fg.get('delta_3d',0):+d}")

    reasons.append(f"[MACRO] VIX={vix:.1f} DXY={macro.get('dxy',101):.2f} "
                   f"US10Y={macro.get('us10y',4.3):.2f}% "
                   f"CPI_YoY={fred_cpi.get('cpi_yoy',3.0):.1f}%")

    if fred_nfp.get("ok"):
        reasons.append(f"[NFP] {fred_nfp.get('nfp_mom',0):+.0f}k emplois/mois "
                       f"({fred_nfp.get('nfp_date','')})")

    if fred_jolts.get("ok"):
        reasons.append(f"[JOLTS] {fred_jolts.get('jolts',0):.1f}M offres "
                       f"Δ={fred_jolts.get('jolts_mom',0):+.2f}M "
                       f"({fred_jolts.get('jolts_date','')})")

    if beige_book.get("detected"):
        reasons.append(f"[BEIGE_BOOK] Ton={beige_book.get('tone','?').upper()} "
                       f"score={beige_book.get('score',0):+.2f} "
                       f"(hawk={beige_book.get('hawk',0)}/dove={beige_book.get('dove',0)})")

    if binance.get("ok"):
        reasons.append(f"[PRIX] BTC=${binance.get('btc',0):,.0f} "
                       f"({binance.get('btc_chg24h',0):+.2f}%) "
                       f"vol={binance.get('btc_vol24h_B',0):.1f}B$")

    if tech_data.get("ok"):
        by_tf = tech_data.get("by_tf", {})
        tf_str = " | ".join([f"RSI{tf}={d['rsi']:.0f}({d['signal']})" for tf, d in by_tf.items()])
        reasons.append(f"[TECH_MTF] {tf_str}")

    reasons.append(f"[SESSION] {session_name} | vol×{vol_mult:.1f} | bias={session_sc:+.2f} | wr_buy={session_wr_buy:.3f}")

    if ff_events:
        high_ev = [e for e in ff_events if e.get("impact") == "HIGH"]
        if high_ev:
            ev0 = high_ev[0]
            reasons.append(f"[FF_HIGH] {ev0.get('currency','?')} — "
                           f"{ev0.get('title','?')[:50]} dans {ev0.get('minutes_until',0)}min")

    if cg.get("ok"):
        reasons.append(f"[CRYPTO_MKT] BTC_DOM={cg.get('btc_dominance',50):.1f}% "
                       f"MCap24h={cg.get('market_chg_24h',0):+.2f}%")

    if fred.get("ok"):
        reasons.append(f"[FED] Taux={fred.get('fed_rate',4.5):.2f}% ({fred.get('fed_date','')})")

    # Mode SCALP vs SWING
    trade_mode = get_trade_mode(sym, final, conviction, news_sc, fg_v, vix, hour_utc)

    # [FIX-11] Macro snapshot complet (risk_off_composite, nasdaq_risk_on, dxy_momentum, tnx_momentum)
    macro_snapshot = compute_macro_snapshot(macro, binance, cg, fred)

    # [FIX-4] Scorecard directionnelle
    scorecard = build_direction_scorecard(
        sym=sym, final_score=final, direction=direction,
        news_sc=news_sc, etf_sc=etf_sc, fg_sc=fg_sc,
        mac_sc=mac_sc,   tech_sc=tech_sc, session_sc=session_sc,
        weights=w, fg=fg, macro=macro, binance=binance,
        tech_data=tech_data, geo_pol=geo_pol_dynamic or profile.get("geo_pol_base", "neutral"),
        session_name=session_name, vol_mult=vol_mult,
        ff_events=ff_events, conviction=conviction,
    )

    return AssetState(
        symbol            = sym,
        direction         = direction,
        score             = final,
        conviction        = conviction,
        volatility_regime = regime,
        force_release     = force_release,
        scores_detail     = {
            "news":       news_sc,  "etf":    etf_sc,
            "fg":         fg_sc,    "macro":  mac_sc,
            "tech":       tech_sc,  "session": session_sc,
            "weights":    w,        "vix":    vix,
            "trade_mode": trade_mode,
            "scorecard":  scorecard,
            "geo_pol_dynamic": geo_pol_dynamic,
            "nfp":        fred_nfp,
            "jolts":      fred_jolts,
            "beige_book": beige_book,
            # [FIX-11] Macro snapshot complet pour le serveur V16
            "macro_snapshot": macro_snapshot,
        },
        reasons      = reasons,
        session_info = {
            "name":           session_name,
            "vol_mult":       vol_mult,
            "bias":           session_sc,
            "wr_buy":         session_wr_buy,
            # camera_allowed = True uniquement si session favorable (wr_buy ≥ 0.52)
            # Bloque CameraOpen quand la session est neutre/défavorable (ex: BTC Tokyo H01)
            "camera_allowed": session_wr_buy >= 0.52,
            "hour_utc":       hour_utc,
            "active":         get_active_sessions(hour_utc),
        },
        last_update = time.time(),
        stale       = False,
    )


# ================================================================================
# WATCHDOG
# ================================================================================

def validate_state(state: AssetState) -> bool:
    if not state.symbol:
        return False
    if state.direction not in ("BUY", "SELL", "NEUTRAL", "RESPIRATION", "CRITICAL"):
        return False
    if not (-1.0 <= state.score <= 1.0):
        return False
    if not (0.0 <= state.conviction <= 1.0):
        return False
    return True


# ================================================================================
# SENTINEL — Boucle de refresh continu
# ================================================================================

class WorldSentinel:
    def __init__(self):
        self._running       = True
        self._last_rss      = 0.0
        self._last_macro    = 0.0
        self._last_macro1h  = 0.0
        self._last_fg       = 0.0
        self._last_cg       = 0.0
        self._last_fred     = 0.0
        self._last_fred_cpi = 0.0
        self._last_fred_jolts = 0.0
        self._last_fred_nfp   = 0.0
        self._last_binance  = 0.0
        self._last_etf:     Dict[str, float] = {}
        self._last_tech:    Dict[str, float] = {}
        self._last_ff       = 0.0
        self._last_push     = 0.0
        self._urgency       = False
        self._cycle_count   = 0
        self._source_health = {k: {"ok": 0, "fail": 0} for k in
                               ["yahoo", "binance", "coingecko", "rss", "fg",
                                "fred", "fred_cpi", "fred_jolts", "fred_nfp"]}

    def _is_stale(self, ts: float, ttl: float) -> bool:
        effective_ttl = ttl * 0.5 if self._urgency else ttl
        return (time.time() - ts) >= effective_ttl

    def _track(self, source: str, ok: bool):
        if source in self._source_health:
            if ok: self._source_health[source]["ok"]   += 1
            else:  self._source_health[source]["fail"] += 1

    async def run(self):
        logger.info("=" * 70)
        logger.info("  WORLD SENTINEL v3.2 — OMEGA FUSION BRAIN — EDITION EA TRADING")
        logger.info("  FIX-8 COMPLET : CPI + NFP + JOLTS + Beige Book")
        logger.info("  ANOMALIE CORRIGÉE : score forex étoffé (Fed/CPI/NFP/JOLTS/DXY)")
        logger.info("  Serveur : %s", SERVER_URL)
        logger.info("  Actifs  : %d | Push : %ds", len(ALL_SYMBOLS), PUSH_INTERVAL)
        logger.info("=" * 70)

        timeout = aiohttp.ClientTimeout(total=15)

        while self._running:
            t0 = time.time()
            self._cycle_count += 1
            updated = []

            try:
                connector = aiohttp.TCPConnector(limit=25, limit_per_host=5)
                async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:

                    # ── RSS + geo_pol dynamique ───────────────────────────────
                    if self._is_stale(self._last_rss, TTL_RSS):
                        headlines = await fetch_rss(session)
                        ok = len(headlines) > 3
                        self._track("rss", ok)
                        if ok:
                            _raw_cache["headlines"] = {"data": headlines, "ts": time.time()}
                            self._last_rss = time.time()
                            updated.append(f"RSS({len(headlines)})")

                            # [FIX-1] Calcul geo_pol dynamique
                            new_geo_pol = _compute_dynamic_geo_pol(headlines)
                            _raw_cache["geo_pol"] = {"data": new_geo_pol, "ts": time.time()}

                            # Détection urgence
                            urgent_kw = ["war ", "invasion", "missile", "nuclear",
                                         "ceasefire broken", "oil shock", "bank run"]
                            cnt = sum(1 for h in headlines[:25] for kw in urgent_kw
                                      if kw in h["text"])
                            prev_urgency   = self._urgency
                            self._urgency  = cnt >= 3
                            if self._urgency and not prev_urgency:
                                logger.warning("[SENTINEL] 🚨 URGENCE GÉOPOLITIQUE — refresh x2")

                    # ── Macro 5min ────────────────────────────────────────────
                    if self._is_stale(self._last_macro, TTL_MACRO):
                        macro = await fetch_macro_yahoo(session, interval="5m")
                        ok = macro.get("ok", False)
                        self._track("yahoo", ok)
                        _raw_cache["macro"] = {"data": macro, "ts": time.time()}
                        self._last_macro = time.time()
                        if ok:
                            updated.append(f"MACRO5m(VIX={macro.get('vix',0):.1f})")

                    # ── [FIX-3] Macro 1h pour analyse session ─────────────────
                    if self._is_stale(self._last_macro1h, TTL_MACRO_1H):
                        macro1h = await fetch_macro_yahoo(session, interval="1h")
                        if macro1h.get("ok"):
                            _raw_cache["macro_1h"] = {"data": macro1h, "ts": time.time()}
                            self._last_macro1h = time.time()
                            updated.append("MACRO1h")

                    # ── Binance prix réels ────────────────────────────────────
                    if self._is_stale(self._last_binance, 60):
                        binance = await fetch_binance_prices(session)
                        ok = binance.get("ok", False)
                        self._track("binance", ok)
                        _raw_cache["binance"] = {"data": binance, "ts": time.time()}
                        self._last_binance = time.time()
                        if ok:
                            updated.append(f"BTC=${binance.get('btc',0):,.0f}")

                    # ── Fear & Greed ──────────────────────────────────────────
                    if self._is_stale(self._last_fg, TTL_FG):
                        fg = await fetch_fear_greed(session)
                        ok = fg.get("ok", False)
                        self._track("fg", ok)
                        _raw_cache["fg"] = {"data": fg, "ts": time.time()}
                        self._last_fg = time.time()
                        if ok:
                            updated.append(f"F&G={fg.get('value',50)}")

                    # ── CoinGecko global ──────────────────────────────────────
                    if self._is_stale(self._last_cg, TTL_CG):
                        cg = await fetch_coingecko_global(session)
                        ok = cg.get("ok", False)
                        self._track("coingecko", ok)
                        _raw_cache["cg"] = {"data": cg, "ts": time.time()}
                        self._last_cg = time.time()
                        if ok:
                            updated.append(f"CG(DOM={cg.get('btc_dominance',50):.0f}%)")

                    # ── [FIX-9] FRED taux Fed (TTL adaptatif FOMC) ────────────
                    fred_ttl = _get_fred_ttl()
                    if self._is_stale(self._last_fred, fred_ttl):
                        fred = await fetch_fred_rate(session)
                        ok = fred.get("ok", False)
                        self._track("fred", ok)
                        _raw_cache["fred"] = {"data": fred, "ts": time.time()}
                        self._last_fred = time.time()
                        if ok:
                            updated.append(f"FED={fred.get('fed_rate',0):.2f}%")
                            if fred_ttl < TTL_FRED:
                                updated.append("⚡FOMC_DAY")

                    # ── [FIX-8 COMPLET] FRED CPI ─────────────────────────────
                    if self._is_stale(self._last_fred_cpi, 3600 * 6):
                        fred_cpi = await fetch_fred_cpi(session)
                        ok = fred_cpi.get("ok", False)
                        self._track("fred_cpi", ok)
                        _raw_cache["fred_cpi"] = {"data": fred_cpi, "ts": time.time()}
                        self._last_fred_cpi = time.time()
                        if ok:
                            updated.append(f"CPI_YoY={fred_cpi.get('cpi_yoy',3.0):.1f}%")

                    # ── [FIX-8 COMPLET] FRED JOLTS offres d'emploi ───────────
                    if self._is_stale(self._last_fred_jolts, 3600 * 12):  # données mensuelles
                        fred_jolts = await fetch_fred_jolts(session)
                        ok = fred_jolts.get("ok", False)
                        self._track("fred_jolts", ok)
                        _raw_cache["fred_jolts"] = {"data": fred_jolts, "ts": time.time()}
                        self._last_fred_jolts = time.time()
                        if ok:
                            updated.append(f"JOLTS={fred_jolts.get('jolts',0):.1f}M")

                    # ── [FIX-8 COMPLET] NFP via FRED PAYEMS ──────────────────
                    if self._is_stale(self._last_fred_nfp, 3600 * 12):    # données mensuelles
                        fred_nfp = await fetch_nfp_bls(session)
                        ok = fred_nfp.get("ok", False)
                        self._track("fred_nfp", ok)
                        _raw_cache["fred_nfp"] = {"data": fred_nfp, "ts": time.time()}
                        self._last_fred_nfp = time.time()
                        if ok:
                            updated.append(f"NFP={fred_nfp.get('nfp_mom',0):+.0f}k")

                    # ── [FIX-8 COMPLET] Beige Book depuis headlines RSS ───────
                    # Recalculé à chaque nouveau cycle RSS (pas de TTL séparé)
                    headlines_now = _raw_cache["headlines"].get("data", [])
                    if headlines_now:
                        bb = detect_beige_book_in_headlines(headlines_now)
                        _raw_cache["beige_book"] = {"data": bb, "ts": time.time()}
                        if bb.get("detected"):
                            updated.append(f"BEIGE_BOOK({bb.get('tone','?').upper()})")

                    # ── ETF par actif (parallèle, [FIX-6] directionnel) ───────
                    etf_tasks = {}
                    for sym, profile in ASSET_PROFILES.items():
                        if self._is_stale(self._last_etf.get(sym, 0.0), TTL_ETF):
                            etfs = profile.get("etf", [])
                            if etfs:
                                etf_tasks[sym] = fetch_etf_flows(session, sym, etfs)

                    if etf_tasks:
                        etf_ress = await asyncio.gather(*etf_tasks.values(), return_exceptions=True)
                        etf_ok = 0
                        for sym, r in zip(etf_tasks.keys(), etf_ress):
                            if isinstance(r, dict):
                                _raw_cache["etf"][sym]  = {"data": r, "ts": time.time()}
                                self._last_etf[sym]     = time.time()
                                if r.get("ok"): etf_ok += 1
                        if etf_ok:
                            updated.append(f"ETF×{etf_ok}(dir)")

                    # ── [FIX-5] RSI multi-timeframe en parallèle ──────────────
                    TTL_TECH = 120
                    tech_tasks = {}
                    for s, p in ASSET_PROFILES.items():
                        if s in YAHOO_SYMBOL_MAP and self._is_stale(self._last_tech.get(s, 0.0), TTL_TECH):
                            tech_tasks[s] = fetch_technicals_for_symbol(session, s, p, ["5m", "1h", "1d"])

                    if tech_tasks:
                        tech_res = await asyncio.gather(*tech_tasks.values(), return_exceptions=True)
                        if "technicals" not in _raw_cache:
                            _raw_cache["technicals"] = {}
                        t_ok = 0
                        for s, r in zip(tech_tasks.keys(), tech_res):
                            if isinstance(r, dict):
                                _raw_cache["technicals"][s] = r
                                self._last_tech[s] = time.time()
                                if r.get("ok"): t_ok += 1
                        if t_ok:
                            updated.append(f"TECH_MTF×{t_ok}")

                    # ── ForexFactory calendrier ───────────────────────────────
                    if self._is_stale(self._last_ff, 1800):
                        ff_ev = await fetch_forexfactory_events(session)
                        _raw_cache["ff_events"] = {"data": ff_ev, "ts": time.time()}
                        self._last_ff = time.time()
                        h_count = sum(1 for e in ff_ev if e.get("impact") == "HIGH")
                        if ff_ev:
                            updated.append(f"FF(H×{h_count})")

                # ── Recalcul états ────────────────────────────────────────────
                if updated:
                    await self._recompute_all()
                    logger.info("[CYCLE %d] %s", self._cycle_count, " | ".join(updated))

                # ── Push vers serveur ─────────────────────────────────────────
                if (time.time() - self._last_push) >= PUSH_INTERVAL:
                    await self._push_to_server()
                    self._last_push = time.time()

                if self._cycle_count % 20 == 0:
                    self._log_source_health()

            except Exception as e:
                logger.error("[SENTINEL] Erreur cycle %d: %s", self._cycle_count, e)

            pause = 2.0 if self._urgency else 5.0
            dt    = time.time() - t0
            await asyncio.sleep(max(0.5, pause - dt))

    async def _recompute_all(self):
        headlines   = _raw_cache["headlines"].get("data", [])
        macro       = _raw_cache["macro"].get("data", {})
        fg          = _raw_cache["fg"].get("data", {"value": 50, "ok": False})
        cg          = _raw_cache["cg"].get("data", {"ok": False})
        fred        = _raw_cache["fred"].get("data", {"fed_rate": 4.5, "ok": False})
        fred_cpi    = _raw_cache["fred_cpi"].get("data", {"cpi_yoy": 3.0, "ok": False})
        fred_jolts  = _raw_cache["fred_jolts"].get("data", {"jolts": 8.0, "ok": False})
        fred_nfp    = _raw_cache["fred_nfp"].get("data", {"nfp_mom": 150, "ok": False})
        beige_book  = _raw_cache["beige_book"].get("data", {"detected": False})
        binance     = _raw_cache["binance"].get("data", {"btc": 95000, "ok": False})
        geo_pol_map = _raw_cache["geo_pol"].get("data", {})

        if not macro and not binance.get("ok"):
            logger.warning("[COMPUTE] Données macro manquantes — skip")
            return

        for sym, profile in ASSET_PROFILES.items():
            try:
                etf_d       = _raw_cache["etf"].get(sym, {}).get("data",
                              {"signal": "NEUTRAL", "ok": False})
                geo_pol_dyn = geo_pol_map.get(sym, profile.get("geo_pol_base", "neutral"))

                new_state = compute_asset_state(
                    sym, profile, headlines, etf_d,
                    fg, macro, binance, cg, fred, fred_cpi,
                    fred_jolts=fred_jolts,
                    fred_nfp=fred_nfp,
                    beige_book=beige_book,
                    geo_pol_dynamic=geo_pol_dyn,
                    urgency=self._urgency,
                )

                if not validate_state(new_state):
                    logger.error("[WATCHDOG] État invalide pour %s — ignoré", sym)
                    continue

                with _shm_lock:
                    SHM_STATE[sym] = new_state

            except Exception as e:
                logger.error("[COMPUTE] %s: %s", sym, e)

        self._log_summary()

    def _log_summary(self):
        with _shm_lock:
            states = dict(SHM_STATE)

        buys  = [s for s, v in states.items() if v.direction == "BUY"]
        sells = [s for s, v in states.items() if v.direction == "SELL"]
        crits = [s for s, v in states.items() if v.direction == "CRITICAL"]
        resps = [s for s, v in states.items() if v.direction == "RESPIRATION"]

        vix   = _raw_cache["macro"].get("data", {}).get("vix", 18) or 18
        btc_p = _raw_cache["binance"].get("data", {}).get("btc", 0)
        fg_v  = _raw_cache["fg"].get("data", {}).get("value", 50)
        cpi   = _raw_cache["fred_cpi"].get("data", {}).get("cpi_yoy", 3.0)
        jolts = _raw_cache["fred_jolts"].get("data", {}).get("jolts", 0)
        nfp   = _raw_cache["fred_nfp"].get("data", {}).get("nfp_mom", 0)
        bb    = _raw_cache["beige_book"].get("data", {})
        hour  = datetime.now(timezone.utc).hour
        sess  = get_active_sessions(hour)

        logger.info("─" * 70)
        logger.info("  Session UTC H%02d : %s", hour, ", ".join(sess))
        if crits: logger.warning("  ⚠️  CRITICAL  : %s", ", ".join(crits))
        if buys:  logger.info("  🟢 BUY       : %s", ", ".join(buys))
        if sells: logger.info("  🔴 SELL      : %s", ", ".join(sells))
        if resps: logger.info("  🔵 RESPIR    : %s", ", ".join(resps))
        logger.info("  VIX=%.1f | BTC=$%s | F&G=%d | CPI_YoY=%.1f%% | Urgence=%s",
                    vix, f"{btc_p:,.0f}" if btc_p else "?", fg_v, cpi, self._urgency)
        if jolts or nfp:
            bb_str = f" | BEIGE_BOOK={bb.get('tone','?').upper()}" if bb.get("detected") else ""
            logger.info("  NFP=%+.0fk/mois | JOLTS=%.1fM offres%s", nfp, jolts, bb_str)
        logger.info("─" * 70)

        for sym, st in states.items():
            if st.stale:
                continue
            icon     = {"BUY": "🟢", "SELL": "🔴", "CRITICAL": "⚠️ ",
                        "RESPIRATION": "🔵", "NEUTRAL": "⬜"}.get(st.direction, "⬜")
            sess_nm  = st.session_info.get("name", "?")
            vol_m    = st.session_info.get("vol_mult", 1.0)
            mode     = st.scores_detail.get("trade_mode", {}).get("mode", "?")
            geo_dyn  = st.scores_detail.get("geo_pol_dynamic", "?")
            r0       = st.reasons[1][:45] if len(st.reasons) > 1 else (st.reasons[0][:45] if st.reasons else "")
            logger.info("  %s %-10s %-13s sc=%+.3f cv=%.0f%% [%s vol×%.1f geo=%s] %s",
                        icon, sym, st.direction, st.score, st.conviction * 100,
                        sess_nm, vol_m, geo_dyn, r0)

    def _log_source_health(self):
        logger.info("[HEALTH] Sources :")
        for src, h in self._source_health.items():
            total = h["ok"] + h["fail"]
            pct   = (h["ok"] / total * 100) if total > 0 else 100.0
            icon  = "✅" if pct >= 80 else ("⚠️ " if pct >= 50 else "❌")
            logger.info("  %s %-12s %.0f%% (%d/%d)", icon, src.upper(), pct, h["ok"], total)

    async def _push_to_server(self):
        """[FIX-10/11/12] Push enrichi : session_info, scorecard, macro_snapshot complet."""
        with _shm_lock:
            results = {}
            for sym, st in SHM_STATE.items():
                if st.stale:
                    continue
                sc = st.scores_detail
                # [FIX-11] Macro snapshot avec risk_off_composite, nasdaq_risk_on, dxy_momentum, tnx_momentum
                macro_snap = sc.get("macro_snapshot", {})
                results[sym] = {
                    "symbol":            st.symbol,
                    "direction":         1 if st.direction == "BUY" else
                                        (-1 if st.direction in ("SELL", "CRITICAL") else 0),
                    "direction_label":   st.direction,
                    "strength":          ("STRONG"   if st.conviction >= 0.70 else
                                         "MODERATE" if st.conviction >= 0.45 else "WEAK"),
                    "decision_score":    st.score,
                    "conviction":        st.conviction,
                    "volatility_regime": st.volatility_regime,
                    "force_release":     st.force_release,
                    "reasons":           st.reasons,
                    "scores":            {
                        "news":    sc.get("news", 0),
                        "etf":     sc.get("etf",  0),
                        "fg":      sc.get("fg",   0),
                        "macro":   sc.get("macro",0),
                        "tech":    sc.get("tech", 0),
                        "session": sc.get("session", 0),
                    },
                    "weights":           sc.get("weights", {}),
                    "trade_mode":        sc.get("trade_mode", {}),
                    # [FIX-10] Session info pour EA (inclut camera_allowed)
                    "session_info":      st.session_info,
                    # Accès direct pour EA — évite double parsing de session_info
                    "camera_allowed":    st.session_info.get("camera_allowed", True),
                    "session_wr_buy":    st.session_info.get("wr_buy", 0.500),
                    # [FIX-4] Scorecard directionnelle complète
                    "scorecard":         sc.get("scorecard", {}),
                    "geo_pol_dynamic":   sc.get("geo_pol_dynamic", "neutral"),
                    # [FIX-8 COMPLET] Données emploi et Beige Book
                    "nfp":               sc.get("nfp", {}),
                    "jolts":             sc.get("jolts", {}),
                    "beige_book":        sc.get("beige_book", {}),
                    # [FIX-12] Macro snapshot au niveau racine — requis par V16 pour AI-32 et cross_asset
                    "macro_snapshot":    macro_snap,
                    # Champs extraits pour accès direct (évite double parsing côté serveur)
                    "risk_off_composite": macro_snap.get("risk_off_composite", 0.5),
                    "nasdaq_risk_on":     macro_snap.get("nasdaq_risk_on",     0.0),
                    "dxy_momentum":       macro_snap.get("dxy_momentum",       0.0),
                    "tnx_momentum":       macro_snap.get("tnx_momentum",       0.0),
                    "risk_regime":        macro_snap.get("risk_regime",    "NEUTRAL"),
                    "source":            "WORLD_SCANNER_v3.2",
                    "timestamp":         datetime.now(timezone.utc).isoformat(),
                    "stale":             False,
                }

        if not results:
            logger.warning("[PUSH] Aucun résultat à pusher")
            return

        payload = {
            "results":   results,
            "scanner":   "WORLD_SCANNER_v3.2",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "urgency":   self._urgency,
        }

        try:
            async with httpx.AsyncClient(timeout=PUSH_TIMEOUT) as c:
                r = await c.post(
                    f"{SERVER_URL}/push_world_scan",
                    json=payload,
                    headers={"Authorization": f"Bearer {API_KEY}",
                             "Content-Type": "application/json"}
                )
                if r.status_code == 200:
                    d = r.json()
                    crit_syms = [s for s, v in results.items() if v.get("force_release")]
                    logger.info("[PUSH] ✅ %d actifs → serveur | injected=%d %s",
                                len(results), d.get("injected", 0),
                                f"| ⚠️  CRITICAL={crit_syms}" if crit_syms else "")
                else:
                    logger.warning("[PUSH] HTTP %d: %s", r.status_code, r.text[:100])
        except Exception as e:
            logger.warning("[PUSH] Erreur: %s — données restent en RAM local", e)


# ================================================================================
# POINT D'ENTRÉE
# ================================================================================

async def main():
    sentinel = WorldSentinel()
    await sentinel.run()

if __name__ == "__main__":
    asyncio.run(main())
