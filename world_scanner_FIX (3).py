# ================================================================================
# world_scanner.py — OMEGA FUSION BRAIN v3.5 — FULL INSTITUTIONAL ENGINE — EDITION INGENIEUR EA TRADING
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
        "TOKYO":         {"bias": -0.044, "vol_mult": 1.25, "wr_buy": 0.478},
        "LONDON":        {"bias": +0.072, "vol_mult": 1.77, "wr_buy": 0.536},
        "NEW_YORK":      {"bias": +0.044, "vol_mult": 1.57, "wr_buy": 0.522},
        "OVERLAP_LN_NY": {"bias": +0.082, "vol_mult": 1.91, "wr_buy": 0.541},
    },
    "XAGUSD": {
        "TOKYO":         {"bias": +0.017, "vol_mult": 1.72, "wr_buy": 0.508},  # H00-H08 — neutre
        "LONDON":        {"bias": -0.075, "vol_mult": 2.33, "wr_buy": 0.462},  # SELL dominant (Comex ouverture)
        "NEW_YORK":      {"bias": -0.135, "vol_mult": 2.06, "wr_buy": 0.433},  # SELL fort — argent suit USD
        "OVERLAP_LN_NY": {"bias": -0.136, "vol_mult": 2.35, "wr_buy": 0.432},  # SELL le plus fort sur XAG
    },

    # ── FOREX MAJEURS ─────────────────────────────────────────────────────
    "EURUSD": {
        "TOKYO":         {"bias": -0.016, "vol_mult": 0.56, "wr_buy": 0.492},
        "LONDON":        {"bias": +0.096, "vol_mult": 0.93, "wr_buy": 0.548},
        "NEW_YORK":      {"bias": +0.036, "vol_mult": 0.79, "wr_buy": 0.518},
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
    "breath":     {},
    "wyckoff":    {"data": {}, "ts": 0.0},
    "correlations": {"data": {}, "ts": 0.0},
    "liquidations": {"data": {}, "ts": 0.0},
    "ofi":        {"data": {}, "ts": 0.0},
    "seasonality": {"data": {}, "ts": 0.0},
    "gdelt":      {"data": {}, "ts": 0.0},
    "fomc":       {"data": {}, "ts": 0.0},
    "pmi":        {"data": {}, "ts": 0.0},
    "twitter":    {"data": {}, "ts": 0.0},
    "onchain":    {"data": {}, "ts": 0.0},
    "newsapi":    {"data": {}, "ts": 0.0},
    "reddit":     {"data": {}, "ts": 0.0},
    "econocal":   {"data": {}, "ts": 0.0},
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

# ================================================================================
# [WS-FIX6] POIDS ADAPTATIFS PAR PERFORMANCE — auto-supervisé
#   Pour chaque cycle, on enregistre le signe de chaque source (news/etf/fg/
#   macro/tech/session) + le prix courant. ~1h plus tard, on compare le signe
#   du score à la direction réelle du prix : correct ou pas. On accumule un
#   taux de réussite glissant par (asset_type, source), et compute_weights_
#   adaptive() multiplie le poids de base par un facteur (0.5 + accuracy),
#   renormalisé -> une source fiable récemment pèse plus, une source qui se
#   trompe souvent pèse moins. Bornes ±30% du poids de base pour éviter
#   qu'une source ne disparaisse complètement ou ne domine tout.
# ================================================================================
SOURCE_PERF_EVAL_HORIZON_SEC = 3600   # 1h : délai avant évaluation
SOURCE_PERF_MAX_SAMPLES      = 100    # fenêtre glissante par (type, source)
SOURCE_PERF_FILE             = "source_perf_state.json"

_SOURCE_LIST   = ["news", "etf", "fg", "macro", "tech", "session"]
_SOURCE_PERF   = {}   # {asset_type: {source: [bool,...]}}
_PENDING_EVAL  = []   # [{ts, sym, asset_type, price, scores:{source:score}}]


def _source_perf_load():
    global _SOURCE_PERF
    try:
        with open(SOURCE_PERF_FILE, "r") as f:
            _SOURCE_PERF = json.load(f)
    except Exception:
        _SOURCE_PERF = {}


def _source_perf_save():
    try:
        with open(SOURCE_PERF_FILE, "w") as f:
            json.dump(_SOURCE_PERF, f)
    except Exception:
        pass


def record_pending_scores(sym: str, asset_type: str, price: float, scores: Dict[str, float]):
    """Appelé à chaque cycle pour un actif : mémorise les scores par source
    avec le prix courant, pour évaluation ~1h plus tard."""
    if price <= 0:
        return
    _PENDING_EVAL.append({
        "ts": time.time(), "sym": sym, "asset_type": asset_type,
        "price": price, "scores": dict(scores),
    })
    # Borne mémoire : ne garde pas plus de quelques milliers d'entrées
    if len(_PENDING_EVAL) > 5000:
        del _PENDING_EVAL[:1000]


def evaluate_pending_scores(current_prices: Dict[str, float]):
    """Appelé à chaque cycle : évalue les entrées arrivées à échéance
    (~1h) en comparant le signe du score à la direction réelle du prix."""
    if not _PENDING_EVAL:
        return
    now = time.time()
    still_pending = []
    updated = False
    for entry in _PENDING_EVAL:
        if now - entry["ts"] < SOURCE_PERF_EVAL_HORIZON_SEC:
            still_pending.append(entry)
            continue

        price_now = current_prices.get(entry["sym"], 0.0)
        if price_now <= 0:
            continue  # pas de prix dispo -> on jette cette entrée (pas bloquante)

        moved_up = price_now > entry["price"]
        moved_down = price_now < entry["price"]
        if not moved_up and not moved_down:
            continue  # prix inchangé -> pas d'info, on jette

        asset_type = entry["asset_type"]
        bucket = _SOURCE_PERF.setdefault(asset_type, {})
        for src, sc in entry["scores"].items():
            if abs(sc) < 1e-6:
                continue  # source neutre -> ne compte ni pour ni contre
            predicted_up = sc > 0
            correct = (predicted_up and moved_up) or (not predicted_up and moved_down)
            lst = bucket.setdefault(src, [])
            lst.append(bool(correct))
            if len(lst) > SOURCE_PERF_MAX_SAMPLES:
                del lst[:len(lst) - SOURCE_PERF_MAX_SAMPLES]
            updated = True

    _PENDING_EVAL[:] = still_pending
    if updated:
        _source_perf_save()


def _source_accuracy(asset_type: str, source: str) -> float:
    """Taux de réussite glissant (0..1). 0.5 = neutre si pas assez de données."""
    lst = _SOURCE_PERF.get(asset_type, {}).get(source, [])
    if len(lst) < 10:
        return 0.5
    return sum(1 for x in lst if x) / len(lst)


def compute_weights_adaptive(vix: float, asset_type: str = "forex", urgency: bool = False) -> Dict[str, float]:
    """compute_weights() de base, ajusté par la performance récente de
    chaque source. Facteur = 0.5 + accuracy, borné à ±30% du poids de base."""
    base = compute_weights(vix, asset_type, urgency)
    adjusted = {}
    for src, w_base in base.items():
        acc = _source_accuracy(asset_type, src)
        factor = 0.5 + acc  # 0.5..1.5 (acc=0 -> 0.5x, acc=1 -> 1.5x, acc=0.5 -> 1.0x)
        w_new = w_base * factor
        # Borne ±30% pour ne jamais faire disparaître/dominer une source
        w_new = max(w_base * 0.7, min(w_base * 1.3, w_new))
        adjusted[src] = w_new

    total = sum(adjusted.values())
    if total <= 0:
        return base
    return {k: round(v / total, 4) for k, v in adjusted.items()}


_source_perf_load()


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


def compute_macro_conviction(fg: Dict, etf_data: Dict, dxy_momentum: float,
                              tnx_momentum: float, fomc_days: int,
                              gdelt_goldstein: float, rsi_1d: float,
                              risk_off_composite: float, oi_signal: str = "NEUTRAL",
                              btc_funding: float = 0.0) -> Dict:
    """
    [V120] MACRO_CONVICTION_GUARD — score agrégé -1 (bearish fort) à +1 (bullish fort)
    indépendant du score technique court-terme (M5/M15).

    Sources utilisées (toutes déjà fetchées par le World Scanner) :
      - Fear & Greed Index (sentiment retail/institutionnel)
      - ETF flows BTC spot (flux institutionnels)
      - DXY momentum (force du dollar — inverse BTC)
      - TNX momentum (taux 10 ans US — inverse actifs risqués)
      - Proximité FOMC/CPI (incertitude macro imminente)
      - GDELT Goldstein (tension géopolitique : négatif = conflit)
      - RSI journalier (survente/surachat structurel)
      - Risk-off composite (appétit pour le risque global)
      - Open Interest signal (positionnement levier)
      - Funding rate signal (sentiment perp futures)

    Stratégie de fusion :
      Chaque source vote -1/0/+1 pondéré, somme normalisée → macro_conviction.
      RSI extrême (≤20 ou ≥80) est traité à part : signal CONTRARIAN
      (capitulation/euphorie → rebond mécanique probable), donc il
      AMORTIT la conviction macro plutôt que de l'accentuer — évite
      de bloquer un BUY juste avant un rebond technique violent.
    """
    votes = []
    reasons = []

    # 1. Fear & Greed — extrême = signal fort (contrarian sur l'extrême, mais
    #    en conviction "tendance" on suit le sentiment dominant à moyen terme)
    fg_val = fg.get("value", 50)
    if fg_val <= 15:
        votes.append((-0.6, 1.0)); reasons.append(f"F&G={fg_val} (Extreme Fear) → bearish dominant")
    elif fg_val <= 30:
        votes.append((-0.35, 1.0)); reasons.append(f"F&G={fg_val} (Fear)")
    elif fg_val >= 85:
        votes.append((+0.6, 1.0)); reasons.append(f"F&G={fg_val} (Extreme Greed) → bullish dominant")
    elif fg_val >= 70:
        votes.append((+0.35, 1.0)); reasons.append(f"F&G={fg_val} (Greed)")
    else:
        votes.append((0.0, 0.5))

    # 2. ETF flows — flux institutionnels BTC spot
    etf_sig = etf_data.get("signal", "NEUTRAL")
    etf_map = {"STRONG_INFLOW": +0.7, "INFLOW": +0.35, "NEUTRAL": 0.0,
               "LOW_VOLUME": -0.05, "OUTFLOW": -0.35, "STRONG_OUTFLOW": -0.7}
    etf_score = etf_map.get(etf_sig, 0.0)
    if etf_score != 0.0:
        votes.append((etf_score, 1.2))  # poids fort : flux réels = argent institutionnel
        reasons.append(f"ETF={etf_sig}")

    # 3. DXY momentum — dollar fort = pression baissière sur BTC
    if abs(dxy_momentum) > 0.15:
        votes.append((-dxy_momentum * 0.8, 1.0))
        reasons.append(f"DXY_mom={dxy_momentum:+.2f}")

    # 4. TNX momentum — taux US en hausse = pression baissière sur actifs risqués
    if abs(tnx_momentum) > 0.15:
        votes.append((-tnx_momentum * 0.6, 0.8))
        reasons.append(f"TNX_mom={tnx_momentum:+.2f}")

    # 5. Proximité FOMC/CPI — incertitude = amplifie le biais déjà présent,
    #    n'a pas de direction propre mais augmente le poids global
    fomc_weight_mult = 1.0
    if fomc_days <= 7:
        fomc_weight_mult = 1.35
        reasons.append(f"FOMC/CPI dans {fomc_days}j → conviction amplifiée")

    # 6. GDELT Goldstein — score géopolitique (négatif = conflit/tension)
    if gdelt_goldstein < -2.0:
        votes.append((-0.5, 1.0)); reasons.append(f"GDELT={gdelt_goldstein:.1f} (tension géopolitique forte)")
    elif gdelt_goldstein < -0.5:
        votes.append((-0.25, 0.6)); reasons.append(f"GDELT={gdelt_goldstein:.1f} (tension géopolitique)")
    elif gdelt_goldstein > 2.0:
        votes.append((+0.3, 0.6))

    # 7. Risk-off composite — 0=risk-on, 1=risk-off total
    if risk_off_composite > 0.65:
        votes.append((-(risk_off_composite - 0.5) * 1.4, 1.0))
        reasons.append(f"risk_off={risk_off_composite:.2f} (flight to safety)")
    elif risk_off_composite < 0.35:
        votes.append(((0.5 - risk_off_composite) * 1.4, 1.0))
        reasons.append(f"risk_on={1-risk_off_composite:.2f}")

    # 8. Open Interest — OI_FLUSH = deleveraging (souvent fin de move = neutre/rebond)
    #    OI_SURGE dans le sens du score technique = confirmation
    if oi_signal == "OI_FLUSH":
        votes.append((0.0, 0.3))  # neutre, amortit — flush = souvent fin de capitulation
        reasons.append("OI_FLUSH → deleveraging, pas de biais directionnel propre")
    elif oi_signal == "OI_SURGE":
        # surge = effet de levier qui s'accumule dans le sens de la tendance dominante
        votes.append((votes[0][0] * 0.3 if votes else 0.0, 0.4))

    # 9. Funding rate brut — funding très négatif = shorts dominants = risque short squeeze (contrarian)
    if btc_funding <= -0.03:
        votes.append((+0.2, 0.5)); reasons.append(f"funding={btc_funding:.4f} (très négatif) → risque short squeeze")
    elif btc_funding >= 0.03:
        votes.append((-0.2, 0.5)); reasons.append(f"funding={btc_funding:.4f} (très positif) → risque long squeeze")

    # ── Fusion pondérée ──
    if votes:
        wsum = sum(w for _, w in votes)
        raw_score = sum(v * w for v, w in votes) / wsum if wsum > 0 else 0.0
    else:
        raw_score = 0.0

    raw_score *= fomc_weight_mult
    raw_score = max(-1.0, min(1.0, raw_score))

    # ── RSI contrarian — AMORTIT la conviction (pas l'inverse) ──
    # RSI ≤20 (capitulation) ou ≥80 (euphorie) → rebond mécanique probable
    # → on réduit la conviction macro de moitié pour ne pas bloquer un BUY
    #   contrarian juste avant un squeeze, ni un SELL avant une chute finale.
    contrarian_damping = False
    if rsi_1d <= 20 and raw_score < 0:
        raw_score *= 0.45
        contrarian_damping = True
        reasons.append(f"RSI_D1={rsi_1d:.1f} (capitulation) → conviction bearish amortie ×0.45")
    elif rsi_1d >= 80 and raw_score > 0:
        raw_score *= 0.45
        contrarian_damping = True
        reasons.append(f"RSI_D1={rsi_1d:.1f} (euphorie) → conviction bullish amortie ×0.45")

    if raw_score <= -0.55:
        label = "MACRO_STRONG_BEARISH"
    elif raw_score <= -0.20:
        label = "MACRO_BEARISH"
    elif raw_score >= 0.55:
        label = "MACRO_STRONG_BULLISH"
    elif raw_score >= 0.20:
        label = "MACRO_BULLISH"
    else:
        label = "MACRO_NEUTRAL"

    return {
        "macro_conviction":     round(raw_score, 3),
        "macro_label":          label,
        "fomc_proximity_days":  fomc_days,
        "contrarian_damping":   contrarian_damping,
        "reasons":              reasons,
    }


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

    # [WS-FIX6] Poids adaptatifs par actif + performance récente par source
    w = compute_weights_adaptive(vix, typ, urgency)

    # [FIX-2] Score news avec geo_pol dynamique
    news_sc, triggers = score_news(headlines, profile, geo_pol_dynamic)

    etf_sc  = score_etf(etf_data)
    fg_sc   = score_fear_greed(fg, profile)
    mac_sc  = score_macro(macro, binance, cg, fred, fred_cpi, profile_with_sym,
                          fred_jolts=fred_jolts, fred_nfp=fred_nfp, beige_book=beige_book)

    # RSI multi-timeframe [FIX-5]
    tech_data = _raw_cache.get("technicals", {}).get(sym, {})
    tech_sc   = float(tech_data.get("score", 0.0)) if tech_data.get("ok") else 0.0

    # [V125-PRECISION-1] Poids dynamique de tech_sc selon alignement multi-timeframe
    # et divergence RSI/prix confirmée. tech_sc est le signal le plus réactif à
    # l'instant T (5m/1h/1d) — il doit peser plus quand il est clair (TF alignés
    # ou divergence haute confiance), et garder son poids de base sinon.
    _tech_weight_boost = 0.0
    _by_tf = tech_data.get("by_tf", {}) if tech_data.get("ok") else {}
    if _by_tf:
        _tf_scores = [d.get("score", 0.0) for d in _by_tf.values() if isinstance(d, dict)]
        if len(_tf_scores) >= 2:
            _tf_signs = [1 if s > 0.05 else (-1 if s < -0.05 else 0) for s in _tf_scores]
            _nonzero  = [s for s in _tf_signs if s != 0]
            if len(_nonzero) >= 2 and all(s == _nonzero[0] for s in _nonzero):
                _tech_weight_boost += 0.05  # TF alignés (5m/1h/1d même sens) → +5% poids

    _divergence = tech_data.get("divergence", {}) if tech_data.get("ok") else {}
    if _divergence.get("signal") in ("BUY", "SELL") and _divergence.get("confidence", 0.0) >= 0.60:
        _tech_weight_boost += 0.05  # Divergence RSI/prix haute confiance → +5% poids

    _tech_weight = min(0.24, w.get("tech", 0.14) + _tech_weight_boost)

    # [FIX-3] Score session horaire + wr_buy pour camera_allowed
    session_sc, session_name, vol_mult, session_wr_buy = get_session_bias_score(sym, hour_utc)

    # [WS-FIX6] Mémorise les scores de cycle pour évaluation différée (~1h)
    _price_now = float(tech_data.get("price", 0)) if tech_data.get("ok") else 0.0
    record_pending_scores(sym, typ, _price_now, {
        "news": news_sc, "etf": etf_sc, "fg": fg_sc,
        "macro": mac_sc, "tech": tech_sc, "session": session_sc,
    })

    # Calendrier ForexFactory
    ff_events  = _raw_cache.get("ff_events", {}).get("data", [])
    ff_penalty = 0.0

    # [WS-NEW] Saisonnalité BTC
    _seas_data  = _raw_cache.get("seasonality", {}).get("data", {})
    _seas_score = float(_seas_data.get("seasonality_score", 0.0)) if typ == "crypto" else 0.0

    # [WS-NEW] OFI impact sur score crypto
    _ofi_data  = _raw_cache.get("ofi", {}).get("data", {})
    _ofi_score = 0.0
    if typ == "crypto" and _ofi_data.get("ok"):
        _ofi_v = float(_ofi_data.get("btc_ofi", 0.0))
        _ofi_score = _ofi_v * 0.30  # OFI → score -0.30 à +0.30

    # [WS-NEW] Liquidations — signal de retournement
    _liq_data  = _raw_cache.get("liquidations", {}).get("data", {})
    _liq_score = 0.0
    if typ == "crypto" and _liq_data.get("ok"):
        _liq_sig = _liq_data.get("liq_signal", "NEUTRAL")
        if _liq_sig == "LONG_FLUSH":   _liq_score =  0.15  # flush = rebond possible
        elif _liq_sig == "SHORT_SQUEEZE": _liq_score = -0.15

    # [WS-NEW] Corrélations — ajuster selon régime
    _correl_data = _raw_cache.get("correlations", {}).get("data", {})
    _correl_adj  = 0.0
    if typ == "crypto" and _correl_data.get("ok"):
        if _correl_data.get("dxy_pressure"): _correl_adj = -0.10
        btc_r = _correl_data.get("btc_regime","INDEPENDENT")
        if btc_r == "RISK_ASSET":
            # Si BTC suit SP500, le score macro SP500 s'applique plus fort
            pass
    for ev in ff_events:
        min_until = ev.get("minutes_until", 999)
        if ev.get("impact") == "HIGH" and min_until < 60:
            ff_penalty = -0.20
            break
        elif ev.get("impact") == "HIGH" and min_until < 240:
            ff_penalty = -0.12
        elif ev.get("impact") == "MEDIUM" and min_until < 30:
            ff_penalty = -0.07

    # Score final pondéré — 10 sources (6 originales + 4 nouvelles)
    final = round(max(-1.0, min(1.0,
        news_sc    * w.get("news",    0.22) +
        etf_sc     * w.get("etf",     0.18) +
        fg_sc      * w.get("fg",      0.10) +
        mac_sc     * w.get("macro",   0.18) +
        tech_sc    * _tech_weight +
        session_sc * w.get("session", 0.06) +
        ff_penalty +
        _seas_score  * 0.06 +   # [WS-NEW] Saisonnalité BTC
        _ofi_score   * 0.04 +   # [WS-NEW] Order Flow Imbalance
        _liq_score   * 0.02 +   # [WS-NEW] Liquidations signal
        _correl_adj           # [WS-NEW] Ajustement corrélation DXY
    )), 4)

    # Direction
    force_release = False
    _fond_dir = 0  # direction de fond (1d), utilisée pour le boost de conviction plus bas
    if final <= SCORE_CRITICAL:
        direction     = "CRITICAL"
        force_release = True
        regime        = "CRITICAL"
    elif final >= SCORE_BUY:
        direction = "BUY"
    elif final <= SCORE_SELL:
        direction = "SELL"
    elif abs(final) <= SCORE_RESPIR:
        # [V125-PRECISION-2] Distinguer vraie respiration (pullback dans une
        # tendance de fond claire) d'une vraie indécision (TF contradictoires,
        # aucune direction de fond) — les deux donnent un score proche de zéro
        # mais correspondent à des situations de trading opposées.
        _1d_score = _by_tf.get("1d", {}).get("score", 0.0) if _by_tf else 0.0
        if abs(_1d_score) >= 0.30:
            direction = "RESPIRATION"
            _fond_dir = 1 if _1d_score > 0 else -1
        else:
            direction = "NEUTRAL"
    else:
        direction = "NEUTRAL"

    conviction = round(min(1.0, abs(final) * 1.8), 2)

    # [V125-PRECISION-3] Divergence RSI majeure → traduction en BUY/SELL fort,
    # PAS un nouveau label "REVERSAL". L'EA (g_OmegaDirection) ne reconnaît que
    # BUY/SELL/NEUTRAL/RESPIRATION/STRONG_BULLISH/STRONG_BEARISH/LIGHT_*/CRITICAL
    # (vérifié : 8+ comparaisons strictes dans STALINE.mq5, ex. lignes 33319-33324,
    # 40534-40548, 52618-52622). Un label inconnu romprait silencieusement ces
    # comparaisons (msaiOmegaOK/omegaOK ne se déclenchent plus, comportement
    # indéterminé). On utilise donc uniquement BULLISH/BEARISH (vrai retournement,
    # prix+RSI divergent dans le sens opposé à la tendance récente) — pas
    # HIDDEN_BULLISH/HIDDEN_BEARISH (continuation, pas retournement, à ne pas
    # confondre) — pour forcer la direction quand le score global était neutre.
    _div_type = _divergence.get("type", "NONE") if _divergence else "NONE"
    _div_conf = _divergence.get("confidence", 0.0) if _divergence else 0.0
    _REVERSAL_CONF_MIN = 0.65  # divergence franche, pas un signal limite

    if direction in ("RESPIRATION", "NEUTRAL") and _div_conf >= _REVERSAL_CONF_MIN:
        if _div_type == "BULLISH":
            direction  = "BUY"
            conviction = round(min(1.0, max(conviction, 0.70) + _div_conf * 0.20), 2)
            _div_forced_reason = f"🔄 RETOURNEMENT (div. RSI BULLISH conf={_div_conf:.2f}) → BUY forcé (score brut={final:+.3f})"
        elif _div_type == "BEARISH":
            direction  = "SELL"
            conviction = round(min(1.0, max(conviction, 0.70) + _div_conf * 0.20), 2)
            _div_forced_reason = f"🔄 RETOURNEMENT (div. RSI BEARISH conf={_div_conf:.2f}) → SELL forcé (score brut={final:+.3f})"
        else:
            _div_forced_reason = None
    else:
        _div_forced_reason = None

    # Boost de conviction (cas non-forçage) : la divergence confirme une
    # direction déjà établie (BUY/SELL franc, ou respiration alignée tendance
    # de fond) → renforcer sans changer la direction.
    if direction in ("BUY", "SELL", "RESPIRATION") and _div_conf > 0 and _div_conf < _REVERSAL_CONF_MIN:
        _div_signal = _divergence.get("signal") if _divergence else None
        if _div_signal in ("BUY", "SELL"):
            _div_dir = 1 if _div_signal == "BUY" else -1
            _ref_dir = _fond_dir if direction == "RESPIRATION" else (1 if final > 0 else (-1 if final < 0 else 0))
            if _ref_dir != 0 and _div_dir == _ref_dir:
                conviction = round(min(1.0, conviction + _div_conf * 0.15), 2)

    # Raisons lisibles
    reasons = []
    if force_release:
        reasons.append(f"⚠️ FORCE_RELEASE — score CRITIQUE={final:.3f}")
    if _div_forced_reason:
        reasons.append(_div_forced_reason)
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

    # ── [CONTRARIAN_RSI] Extraction RSI daily pour détection capitulation/euphorie ──
    # Le MQ5 lit rsi_1d depuis le payload pour voter BUY en zone de capitulation
    # (BTC RSI_D1=18 le 4 juin 2026 → rebond +5% en 2h ignoré par EMAs bearish)
    rsi_1d = 50.0
    if tech_data.get("ok"):
        by_tf = tech_data.get("by_tf", {})
        rsi_1d = by_tf.get("1d", by_tf.get("1h", {})).get("rsi", 50.0)

    # Détection zone contrariante — ajuste direction et camera_allowed
    contrarian_zone = False
    contrarian_dir  = ""
    if rsi_1d <= 22.0:
        contrarian_zone = True
        contrarian_dir  = "BUY"   # Capitulation → rebond probable
        reasons.append(f"[CONTRARIAN] RSI_D1={rsi_1d:.1f} ≤ 22 → CAPITULATION — BUY contrarian probable")
    elif rsi_1d >= 78.0:
        contrarian_zone = True
        contrarian_dir  = "SELL"  # Euphorie → retournement probable
        reasons.append(f"[CONTRARIAN] RSI_D1={rsi_1d:.1f} ≥ 78 → EUPHORIE — SELL contrarian probable")

    # Si contrarian fort et direction actuelle opposée → forcer la direction contrariante
    # (seulement si score est ambigu, pas si signal très fort dans l'autre sens)
    # [V125-PRECISION] Ne pas écraser un forçage par divergence RSI/prix déjà
    # appliqué plus haut (_div_forced_reason) — éviter que deux mécanismes de
    # retournement (RSI absolu vs divergence RSI/prix) se contredisent en silence.
    if contrarian_zone and abs(final) < 0.50 and not _div_forced_reason:
        direction = contrarian_dir

    # camera_allowed : bloquer si RSI_D1 en zone extrême ET direction = sens du trend épuisé
    # Ex: SELL camera sur BTC avec RSI_D1=18 → BLOQUÉ (risque rebond mécanique)
    session_cam_ok = session_wr_buy >= 0.52
    rsi_cam_ok     = True
    if contrarian_zone and direction != contrarian_dir:
        rsi_cam_ok = False  # On essaie d'ouvrir dans le sens épuisé → bloquer

    camera_ok = session_cam_ok and rsi_cam_ok

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
            "price":      _price_now,  # [WS-FIX6] pour évaluation adaptative
            "trade_mode": trade_mode,
            "scorecard":  scorecard,
            "geo_pol_dynamic": geo_pol_dynamic,
            "nfp":        fred_nfp,
            "jolts":      fred_jolts,
            "beige_book": beige_book,
            # [FIX-11] Macro snapshot complet pour le serveur V16
            "macro_snapshot": macro_snapshot,
            # [CONTRARIAN_RSI] Pour debug
            "rsi_1d":         round(rsi_1d, 1),
            "contrarian_zone": contrarian_zone,
            "contrarian_dir":  contrarian_dir,
        },
        reasons      = reasons,
        session_info = {
            "name":            session_name,
            "vol_mult":        vol_mult,
            "bias":            session_sc,
            "wr_buy":          session_wr_buy,
            # [CAMERA_GUARD] camera_allowed : session ET RSI extrême vérifiés
            "camera_allowed":  camera_ok,
            "camera_session_ok": session_cam_ok,
            "camera_rsi_ok":   rsi_cam_ok,
            "hour_utc":        hour_utc,
            "active":          get_active_sessions(hour_utc),
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



# ================================================================================
# [GEO-ECO+] SOURCES GÉOPOLITIQUES ET ÉCONOMIQUES AVANCÉES — v1.0
# GDELT Project | FOMC live | PMI/ISM TradingEconomics | Twitter/X impact
# Zéro doublon avec RSS, FRED, ForexFactory, macro_yahoo existants
# Endpoints serveur inchangés — données injectées dans les clés existantes
# ================================================================================

TTL_GDELT   = 300    # GDELT — 5 minutes (événements géopolitiques)
TTL_FOMC    = 600    # FOMC calendrier — 10 minutes
TTL_PMI     = 1800   # PMI/ISM — 30 minutes (données lentes)
TTL_TWITTER  = 120    # Twitter/X impact — 2 minutes (très volatile)
TTL_ONCHAIN  = 300    # On-chain metrics blockchain.info + mempool.space — 5 min
TTL_NEWSAPI  = 180    # NewsAPI.org headlines sentiment — 3 min
TTL_REDDIT   = 240    # Reddit r/CryptoCurrency + r/Bitcoin — 4 min
TTL_ECONOCAL = 1800   # ForexFactory RSS economic calendar — 30 min

# ── GDELT Project — base données géopolitiques mondiale (totalement gratuit) ──
async def fetch_gdelt(session: aiohttp.ClientSession) -> Dict:
    """
    GDELT Project : analyse 100+ médias mondiaux en temps réel.
    API gratuite sans clé. Retourne le Goldstein Scale (tension géopolitique)
    et le tone moyen des articles sur BTC/Gold/USD des dernières 15 minutes.
    Signal :
      goldstein < -5  → tension extrême → risk-off → gold BUY / crypto SELL
      goldstein > +3  → détente → risk-on → crypto BUY / gold neutre
      tone < -3       → sentiment négatif dominant → bear
      tone > +2       → sentiment positif → bull
    """
    result = {
        "goldstein":     0.0,   # -10 (guerre) à +10 (paix)
        "tone":          0.0,   # ton médiatique moyen
        "num_articles":  0,
        "top_themes":    [],
        "risk_signal":   "NEUTRAL",
        "geo_score":     0.0,
        "ok": False, "source": "fallback"
    }
    try:
        # GDELT DOC API — articles financiers dernières 15min
        url = ("https://api.gdeltproject.org/api/v2/doc/doc?"
               "query=bitcoin%20OR%20gold%20OR%20dollar%20OR%20fed%20OR%20inflation"
               "&mode=artlist&maxrecords=25&format=json"
               "&timespan=15min&sort=DateDesc")
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10),
                               headers=H_BROWSER) as r:
            if r.status == 200:
                d = await r.json(content_type=None)
                articles = d.get("articles", [])
                if articles:
                    tones      = [float(a.get("tone",      0) or 0) for a in articles]
                    goldsteins = [float(a.get("goldstein", 0) or 0) for a in articles]
                    result["num_articles"] = len(articles)
                    result["tone"]         = round(sum(tones) / len(tones), 2) if tones else 0.0
                    result["goldstein"]    = round(sum(goldsteins) / len(goldsteins), 2) if goldsteins else 0.0
                    # Thèmes dominants
                    themes = []
                    for a in articles[:5]:
                        t = a.get("title", "")
                        if t: themes.append(t[:60])
                    result["top_themes"] = themes

                    # Signal
                    g = result["goldstein"]
                    t_v = result["tone"]
                    score = 0.0
                    if   g < -5:  score -= 0.35   # tension extrême
                    elif g < -2:  score -= 0.15
                    elif g >  3:  score += 0.20   # détente
                    if   t_v < -3: score -= 0.15
                    elif t_v >  2: score += 0.10

                    result["geo_score"]  = round(max(-0.50, min(0.50, score)), 3)
                    result["risk_signal"] = (
                        "RISK_OFF" if score < -0.15 else
                        "RISK_ON"  if score >  0.15 else
                        "NEUTRAL"
                    )
                    result["ok"]     = True
                    result["source"] = "gdelt"
    except Exception:
        pass

    # Fallback : GDELT GKG (Global Knowledge Graph) — plus léger
    if not result["ok"]:
        try:
            url2 = ("https://api.gdeltproject.org/api/v2/tv/tv?"
                    "query=bitcoin%20gold%20fed&mode=clipgallery&maxclips=5"
                    "&format=json&timespan=60min")
            async with session.get(url2, timeout=aiohttp.ClientTimeout(total=8),
                                   headers=H_BROWSER) as r:
                if r.status == 200:
                    result["ok"]     = True
                    result["source"] = "gdelt_tv"
        except Exception:
            pass

    return result


async def fetch_fomc_calendar(session: aiohttp.ClientSession) -> Dict:
    """
    Calendrier FOMC et discours Fed live.
    Sources : FedSpeech RSS + Fed Reserve website calendar.
    Signal :
      "hawkish" dans titre Powell → USD BUY / or SELL / crypto SELL
      "dovish" ou "rate cut" → USD SELL / or BUY / crypto BUY
      réunion FOMC dans <24h → volatilité extrême → lot × 0.5
    """
    result = {
        "next_meeting_days": 999,
        "next_meeting_date": "",
        "fed_tone":          "NEUTRAL",   # HAWKISH / DOVISH / NEUTRAL
        "fed_score":         0.0,
        "recent_headlines":  [],
        "fomc_urgency":      False,       # True si réunion dans <24h
        "ok": False, "source": "fallback"
    }

    # ── RSS Fed Reserve speeches ────────────────────────────────────────────
    try:
        url = "https://www.federalreserve.gov/feeds/speeches.xml"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8),
                               headers=H_BROWSER) as r:
            if r.status == 200:
                text = await r.text()
                import re
                titles = re.findall(r'<title><!\[CDATA\[(.*?)\]\]></title>', text)
                if not titles:
                    titles = re.findall(r'<title>(.*?)</title>', text)

                hawkish_kw = ["rate hike", "hawkish", "tighten", "inflation concern",
                              "restrictive", "above target", "elevated inflation",
                              "higher for longer", "not yet confident"]
                dovish_kw  = ["rate cut", "dovish", "easing", "confident inflation",
                               "approaching target", "balanced risks", "labor market cool",
                               "appropriate to cut", "pivot"]

                h_count = d_count = 0
                headlines = []
                for t in titles[:8]:
                    t_low = t.lower()
                    h_count += sum(1 for kw in hawkish_kw if kw in t_low)
                    d_count += sum(1 for kw in dovish_kw  if kw in t_low)
                    headlines.append(t[:80])

                result["recent_headlines"] = headlines[:5]
                if   h_count > d_count and h_count > 0:
                    result["fed_tone"]  = "HAWKISH"
                    result["fed_score"] = -min(0.30, h_count * 0.08)
                elif d_count > h_count and d_count > 0:
                    result["fed_tone"]  = "DOVISH"
                    result["fed_score"] = min(0.25, d_count * 0.07)
                else:
                    result["fed_tone"]  = "NEUTRAL"
                    result["fed_score"] = 0.0

                result["ok"]     = True
                result["source"] = "fed_rss"
    except Exception:
        pass

    # ── FOMC meeting dates 2026 (hardcodé — change 1x/an) ──────────────────
    from datetime import date, datetime as dt2
    fomc_dates_2026 = [
        date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29),
        date(2026, 6, 17), date(2026, 7, 29), date(2026, 9, 16),
        date(2026, 10, 28), date(2026, 12, 9),
    ]
    today = date.today()
    future = [d for d in fomc_dates_2026 if d >= today]
    if future:
        delta = (future[0] - today).days
        result["next_meeting_days"] = delta
        result["next_meeting_date"] = str(future[0])
        result["fomc_urgency"]      = delta <= 1

    return result


async def fetch_pmi_ism(session: aiohttp.ClientSession) -> Dict:
    """
    PMI / ISM Manufacturing + Services via sources publiques.
    ISM > 50 = expansion = risk-on
    ISM < 50 = contraction = risk-off
    Sources : FRED (ISM via M_PMI serie) + scrape léger TradingEconomics
    """
    result = {
        "us_pmi_mfg":     50.0,   # ISM Manufacturing
        "us_pmi_svc":     50.0,   # ISM Services
        "us_pmi_comp":    50.0,   # Composite
        "eu_pmi_comp":    50.0,   # Eurozone composite
        "pmi_signal":     "NEUTRAL",
        "pmi_score":      0.0,
        "ok": False, "source": "fallback"
    }

    # ── FRED MANEMP / NAPMPMI (ISM Manufacturing PMI) ────────────────────────
    try:
        fred_key = os.getenv("FRED_API_KEY", "")
        if fred_key:
            for serie, key in [("NAPMPMI", "us_pmi_mfg"), ("NMFCI", "us_pmi_svc")]:
                url = (f"https://api.stlouisfed.org/fred/series/observations"
                       f"?series_id={serie}&api_key={fred_key}&sort_order=desc"
                       f"&limit=1&file_type=json")
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=6),
                                       headers=H_BOT) as r:
                    if r.status == 200:
                        d = await r.json(content_type=None)
                        obs = d.get("observations", [])
                        if obs:
                            try:
                                result[key] = float(obs[0]["value"])
                                result["ok"] = True
                                result["source"] = "fred_pmi"
                            except Exception:
                                pass
    except Exception:
        pass

    # ── Fallback : Yahoo Finance PMI ETF proxy ────────────────────────────────
    if not result["ok"]:
        try:
            # XLI = Industrial ETF — proxy PMI manufacturing
            url = "https://query1.finance.yahoo.com/v8/finance/chart/XLI?range=5d&interval=1d"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=6),
                                   headers=H_BOT) as r:
                if r.status == 200:
                    d = await r.json(content_type=None)
                    closes = d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                    closes = [c for c in closes if c]
                    if len(closes) >= 2:
                        chg = (closes[-1] - closes[-2]) / closes[-2] * 100
                        # XLI monte = confiance industrielle = PMI-like > 50
                        result["us_pmi_mfg"] = 50.0 + chg * 2
                        result["ok"]         = True
                        result["source"]     = "xli_proxy"
        except Exception:
            pass

    # ── Signal ───────────────────────────────────────────────────────────────
    mfg = result["us_pmi_mfg"]
    svc = result["us_pmi_svc"]
    comp = (mfg * 0.4 + svc * 0.6)
    result["us_pmi_comp"] = round(comp, 1)
    score = 0.0
    if   comp > 55: score =  0.20
    elif comp > 52: score =  0.10
    elif comp < 45: score = -0.20
    elif comp < 48: score = -0.10
    result["pmi_score"]  = round(score, 3)
    result["pmi_signal"] = (
        "EXPANSION_STRONG" if comp > 55 else
        "EXPANSION"        if comp > 52 else
        "CONTRACTION"      if comp < 48 else
        "CONTRACTION_STRONG" if comp < 45 else
        "NEUTRAL"
    )
    return result


async def fetch_twitter_impact(session: aiohttp.ClientSession) -> Dict:
    """
    Impact Twitter/X sur BTC — via sources publiques (pas de clé API X).
    Sources : LunarCrush public lite + CryptoPanic public + scrape Nitter.
    Signal :
      social_volume spike → mouvement prix imminent (direction = sentiment)
      bullish_mentions > 70% → BUY pressure sociale
      bearish_mentions > 70% → SELL pressure sociale
    Note : Trump/Elon tweets capturés via RSS Nitter (miroir X gratuit)
    """
    result = {
        "btc_social_volume":    0,
        "btc_bullish_pct":      50.0,
        "btc_bearish_pct":      50.0,
        "vip_alert":            False,    # Tweet Trump/Elon détecté
        "vip_sentiment":        "NEUTRAL",
        "social_signal":        "NEUTRAL",
        "social_score":         0.0,
        "ok": False, "source": "fallback"
    }

    # ── CryptoPanic public API (gratuit, pas de clé) ─────────────────────────
    try:
        url = "https://cryptopanic.com/api/free/v1/posts/?auth_token=free&filter=hot&currencies=BTC"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8),
                               headers=H_BROWSER) as r:
            if r.status == 200:
                d = await r.json(content_type=None)
                posts = d.get("results", [])
                if posts:
                    bull = sum(1 for p in posts if p.get("kind") == "news" and
                               any(kw in (p.get("title","") or "").lower()
                                   for kw in ["surge","rally","ath","bullish","buy","breakout","pump"]))
                    bear = sum(1 for p in posts if p.get("kind") == "news" and
                               any(kw in (p.get("title","") or "").lower()
                                   for kw in ["crash","dump","sell","bearish","fall","drop","fear"]))
                    total_s = bull + bear
                    if total_s > 0:
                        result["btc_bullish_pct"] = round(bull / total_s * 100, 1)
                        result["btc_bearish_pct"] = round(bear / total_s * 100, 1)
                    result["btc_social_volume"] = len(posts)
                    result["ok"]     = True
                    result["source"] = "cryptopanic"
    except Exception:
        pass

    # ── Nitter RSS (miroir Twitter gratuit) — Trump + Elon ───────────────────
    trump_kw_bull = ["bitcoin", "btc", "crypto", "gold", "buy", "reserve", "strategic"]
    trump_kw_bear = ["ban", "crash", "fraud", "ponzi", "dangerous", "regulate"]
    elon_kw_bull  = ["bitcoin", "btc", "doge", "crypto", "moon", "hodl"]
    elon_kw_bear  = ["sell", "dump", "fraud", "over", "done"]

    vip_detected = False
    vip_bull = vip_bear = 0

    for nitter_url in [
        "https://nitter.net/realDonaldTrump/rss",
        "https://nitter.net/elonmusk/rss",
        "https://nitter.poast.org/realDonaldTrump/rss",
        "https://nitter.poast.org/elonmusk/rss",
    ]:
        try:
            async with session.get(nitter_url, timeout=aiohttp.ClientTimeout(total=5),
                                   headers=H_BROWSER) as r:
                if r.status == 200:
                    text = await r.text()
                    import re
                    items = re.findall(r'<title>(.*?)</title>', text, re.DOTALL)
                    for item in items[1:6]:  # skip feed title
                        item_low = item.lower()
                        b = sum(1 for kw in trump_kw_bull + elon_kw_bull if kw in item_low)
                        be = sum(1 for kw in trump_kw_bear + elon_kw_bear if kw in item_low)
                        if b >= 1 or be >= 1:
                            vip_detected = True
                            vip_bull += b
                            vip_bear += be
                    break  # premier qui répond suffit
        except Exception:
            continue

    if vip_detected:
        result["vip_alert"]     = True
        result["vip_sentiment"] = "BULLISH" if vip_bull > vip_bear else "BEARISH" if vip_bear > vip_bull else "NEUTRAL"

    # ── Score social composite ────────────────────────────────────────────────
    score = 0.0
    bull_pct = result["btc_bullish_pct"]
    if   bull_pct > 70: score =  0.15
    elif bull_pct > 60: score =  0.08
    elif bull_pct < 30: score = -0.15
    elif bull_pct < 40: score = -0.08

    if result["vip_sentiment"] == "BULLISH":  score += 0.12
    elif result["vip_sentiment"] == "BEARISH": score -= 0.12

    result["social_score"]  = round(max(-0.30, min(0.30, score)), 3)
    result["social_signal"] = (
        "BULL_SOCIAL" if score >  0.10 else
        "BEAR_SOCIAL" if score < -0.10 else
        "NEUTRAL"
    )
    return result


def enrich_geo_pol_with_advanced(geo_pol_map: Dict,
                                  gdelt: Dict, fomc: Dict,
                                  pmi: Dict, twitter: Dict) -> Dict:
    """
    Enrichit le geo_pol_map existant avec les nouvelles sources.
    NE REMPLACE PAS — ajoute un score composite additionnel.
    Le serveur lit toujours geo_pol_dynamic (clé inchangée).
    On ajoute geo_pol_advanced pour signal supplémentaire sans casser l'existant.
    """
    scores = {
        "gdelt":   gdelt.get("geo_score",   0.0) * 0.35,
        "fomc":    fomc.get("fed_score",    0.0) * 0.25,
        "pmi":     pmi.get("pmi_score",     0.0) * 0.20,
        "twitter": twitter.get("social_score", 0.0) * 0.20,
    }
    total = sum(scores.values())

    signal = (
        "RISK_OFF_STRONG" if total < -0.20 else
        "RISK_OFF"        if total < -0.08 else
        "RISK_ON"         if total >  0.08 else
        "RISK_ON_STRONG"  if total >  0.20 else
        "NEUTRAL"
    )

    reasons = []
    if gdelt.get("ok"):
        reasons.append(f"GDELT goldstein={gdelt.get('goldstein',0):+.1f} tone={gdelt.get('tone',0):+.1f} → {gdelt.get('risk_signal')}")
    if fomc.get("ok"):
        reasons.append(f"FED {fomc.get('fed_tone')} | next_FOMC={fomc.get('next_meeting_days')}j")
        if fomc.get("fomc_urgency"):
            reasons.append("⚠️ FOMC DEMAIN — volatilité extrême attendue")
    if pmi.get("ok"):
        reasons.append(f"PMI_comp={pmi.get('us_pmi_comp',50):.1f} → {pmi.get('pmi_signal')}")
    if twitter.get("vip_alert"):
        reasons.append(f"VIP_TWEET détecté → {twitter.get('vip_sentiment')}")

    return {
        "advanced_score":   round(total, 3),
        "advanced_signal":  signal,
        "advanced_reasons": reasons,
        "fomc_urgency":     fomc.get("fomc_urgency", False),
        "fomc_days":        fomc.get("next_meeting_days", 999),
        "fed_tone":         fomc.get("fed_tone", "NEUTRAL"),
        "pmi_comp":         pmi.get("us_pmi_comp", 50.0),
        "pmi_signal":       pmi.get("pmi_signal", "NEUTRAL"),
        "gdelt_goldstein":  gdelt.get("goldstein", 0.0),
        "social_signal":    twitter.get("social_signal", "NEUTRAL"),
        "vip_alert":        twitter.get("vip_alert", False),
        "scores":           {k: round(v, 3) for k, v in scores.items()},
    }


# ================================================================================
# [BREATH] MODULE RESPIRATIONS INSTITUTIONNELLES — v1.0
# Sources : Funding Rate, Long/Short Ratio, Open Interest, CVD, CME Gaps, Deribit
# Logique : détecte les zones de respiration AVANT qu'elles arrivent
# Zéro doublon avec l'existant (ne refait pas prix, F&G, ETF, macro FRED, RSI)
# ================================================================================

TTL_WYCKOFF        = 600
TTL_CORREL         = 1800
TTL_LIQUIDATIONS   = 30
TTL_OFI            = 30
TTL_BREATH_FAST  = 60    # Funding / L/S ratio — données temps réel
TTL_BREATH_OI    = 120   # Open Interest — moins volatile
TTL_BREATH_CME   = 3600  # CME gaps — change rarement
TTL_BREATH_CVD   = 90    # CVD klines binance — scalp
TTL_BREATH_DERIV = 300   # Deribit options — change toutes les 5min

async def fetch_funding_longshort(session: aiohttp.ClientSession) -> Dict:
    """
    Funding Rate + Long/Short Ratio retail (Binance/CryptoCompare/Bybit public).
    Signal respiration :
      funding > +0.03%  → marché suracheté → respiration SELL imminente
      funding < -0.01%  → marché survendu  → respiration BUY imminente
      L/S retail > 65%  long → institutionnel contra → respiration SELL
      L/S retail < 35%  long → institutionnel contra → respiration BUY
    """
    result = {
        "btc_funding":      0.0,
        "eth_funding":      0.0,
        "btc_ls_long_pct":  50.0,
        "btc_ls_short_pct": 50.0,
        "breath_signal":    "NEUTRAL",
        "breath_score":     0.0,
        "ok": False, "source": "fallback"
    }

    # ── Source 1 : Bybit V5 public (pas de clé, pas de ban IP) ──────────────
    try:
        url_f = "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT"
        async with session.get(url_f, timeout=aiohttp.ClientTimeout(total=6),
                               headers=H_BOT) as r:
            if r.status == 200:
                d = await r.json(content_type=None)
                items = d.get("result", {}).get("list", [])
                if items:
                    result["btc_funding"] = float(items[0].get("fundingRate", 0.0))
                    result["ok"]     = True
                    result["source"] = "bybit"
    except Exception:
        pass

    # ── Source 2 : Binance funding (si Bybit KO) ────────────────────────────
    if not result["ok"]:
        try:
            url_f = "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"
            async with session.get(url_f, timeout=aiohttp.ClientTimeout(total=6),
                                   headers=H_CURL) as r:
                if r.status == 200:
                    d = await r.json(content_type=None)
                    result["btc_funding"] = float(d.get("lastFundingRate", 0.0))
                    result["ok"]     = True
                    result["source"] = "binance_fapi"
        except Exception:
            pass

    # ── Long/Short Ratio retail (Bybit V5) ───────────────────────────────────
    try:
        url_ls = "https://api.bybit.com/v5/market/account-ratio?category=linear&symbol=BTCUSDT&period=1h&limit=1"
        async with session.get(url_ls, timeout=aiohttp.ClientTimeout(total=6),
                               headers=H_BOT) as r:
            if r.status == 200:
                d = await r.json(content_type=None)
                lst = d.get("result", {}).get("list", [])
                if lst:
                    result["btc_ls_long_pct"]  = float(lst[0].get("buyRatio",  0.5)) * 100
                    result["btc_ls_short_pct"] = float(lst[0].get("sellRatio", 0.5)) * 100
    except Exception:
        pass

    # ── ETH funding (Bybit) ──────────────────────────────────────────────────
    try:
        url_e = "https://api.bybit.com/v5/market/tickers?category=linear&symbol=ETHUSDT"
        async with session.get(url_e, timeout=aiohttp.ClientTimeout(total=5),
                               headers=H_BOT) as r:
            if r.status == 200:
                d = await r.json(content_type=None)
                items = d.get("result", {}).get("list", [])
                if items:
                    result["eth_funding"] = float(items[0].get("fundingRate", 0.0))
    except Exception:
        pass

    # ── Calcul signal respiration ────────────────────────────────────────────
    f   = result["btc_funding"]
    ls  = result["btc_ls_long_pct"]
    score = 0.0

    # Funding : suracheté (>0.03%) → SELL ; survendu (<-0.01%) → BUY
    if   f > 0.0003:  score -= min(0.40, (f - 0.0003) * 1000)   # bearish pressure
    elif f < -0.0001: score += min(0.30, (-f - 0.0001) * 800)   # bullish pressure

    # Long/Short : retail 70%+ long = institutionnel SELL
    if   ls > 70: score -= (ls - 70) * 0.015
    elif ls < 35: score += (35 - ls) * 0.012

    result["breath_score"]  = round(max(-1.0, min(1.0, score)), 3)
    result["breath_signal"] = (
        "SELL_BREATH" if score < -0.15 else
        "BUY_BREATH"  if score >  0.15 else
        "NEUTRAL"
    )
    return result


async def fetch_open_interest(session: aiohttp.ClientSession) -> Dict:
    """
    Open Interest BTC+ETH (Coinglass public / Bybit public).
    Signal respiration :
      OI monte + prix stagne → accumulation → explosion imminente (direction = funding)
      OI chute fort          → deleveraging → fin de tendance → respiration contra
      OI_change_1h > +5%     → surge → watch
    """
    result = {
        "btc_oi_usd":      0.0,
        "btc_oi_chg_1h":   0.0,
        "eth_oi_usd":      0.0,
        "oi_signal":       "NEUTRAL",
        "oi_score":        0.0,
        "ok": False, "source": "fallback"
    }

    # ── Bybit V5 OI (gratuit, pas de clé) ───────────────────────────────────
    try:
        for sym_b, key in [("BTCUSDT", "btc"), ("ETHUSDT", "eth")]:
            url = f"https://api.bybit.com/v5/market/open-interest?category=linear&symbol={sym_b}&intervalTime=1h&limit=2"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=7),
                                   headers=H_BOT) as r:
                if r.status == 200:
                    d = await r.json(content_type=None)
                    lst = d.get("result", {}).get("list", [])
                    if len(lst) >= 2:
                        oi_now  = float(lst[0].get("openInterest", 0))
                        oi_prev = float(lst[1].get("openInterest", 0))
                        result[f"{key}_oi_usd"]    = round(oi_now / 1e9, 3)   # milliards
                        if oi_prev > 0:
                            result[f"{key}_oi_chg_1h"] = round((oi_now - oi_prev) / oi_prev * 100, 2)
                        result["ok"]     = True
                        result["source"] = "bybit_oi"
    except Exception:
        pass

    # ── Signal OI ────────────────────────────────────────────────────────────
    chg = result["btc_oi_chg_1h"]
    if   chg >  5.0: result["oi_signal"] = "OI_SURGE"      ; result["oi_score"] =  0.20
    elif chg >  2.0: result["oi_signal"] = "OI_BUILDING"   ; result["oi_score"] =  0.10
    elif chg < -5.0: result["oi_signal"] = "OI_FLUSH"      ; result["oi_score"] = -0.25
    elif chg < -2.0: result["oi_signal"] = "OI_DECLINING"  ; result["oi_score"] = -0.12
    else:            result["oi_signal"] = "NEUTRAL"        ; result["oi_score"] =  0.0

    return result


async def fetch_cvd(session: aiohttp.ClientSession) -> Dict:
    """
    CVD (Cumulative Volume Delta) via Bybit klines agressions buy/sell.
    Proxy : klines 1min sur 60 bougies → buyVol vs sellVol → delta cumulé
    Signal respiration :
      CVD monte + prix baisse → divergence → BUY breath imminente
      CVD baisse + prix monte → divergence → SELL breath imminente
      CVD confirme prix       → tendance pure, pas de respiration
    """
    result = {
        "btc_cvd_bias":   0.0,      # +1.0 = pure buy, -1.0 = pure sell
        "btc_divergence": False,     # CVD diverge du prix
        "cvd_signal":     "NEUTRAL",
        "cvd_score":      0.0,
        "ok": False, "source": "fallback"
    }

    try:
        url = "https://api.bybit.com/v5/market/kline?category=linear&symbol=BTCUSDT&interval=1&limit=60"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8),
                               headers=H_BOT) as r:
            if r.status == 200:
                d = await r.json(content_type=None)
                lst = d.get("result", {}).get("list", [])
                # Bybit kline format: [timestamp, open, high, low, close, volume, turnover]
                if len(lst) >= 10:
                    closes   = [float(k[4]) for k in lst]
                    volumes  = [float(k[5]) for k in lst]
                    opens_p  = [float(k[1]) for k in lst]
                    # Proxy CVD : bougie haussière = buy vol, baissière = sell vol
                    buy_vols  = [v if c > o else 0 for c, o, v in zip(closes, opens_p, volumes)]
                    sell_vols = [v if c < o else 0 for c, o, v in zip(closes, opens_p, volumes)]
                    total_buy  = sum(buy_vols)
                    total_sell = sum(sell_vols)
                    total      = total_buy + total_sell
                    if total > 0:
                        bias = (total_buy - total_sell) / total  # -1 à +1
                        result["btc_cvd_bias"] = round(bias, 3)

                    # Divergence : CVD vs prix (30 premières vs 30 dernières bougies)
                    mid = len(lst) // 2
                    price_trend = closes[-1] - closes[mid]     # prix monté ou baissé
                    buy_trend   = sum(buy_vols[-mid:]) - sum(buy_vols[:mid])
                    # Divergence si prix monte mais buy vol baisse (ou inverse)
                    if price_trend > 0 and buy_trend < 0:
                        result["btc_divergence"] = True
                        result["cvd_signal"]     = "SELL_DIVERGENCE"
                        result["cvd_score"]      = -0.30
                    elif price_trend < 0 and buy_trend > 0:
                        result["btc_divergence"] = True
                        result["cvd_signal"]     = "BUY_DIVERGENCE"
                        result["cvd_score"]      =  0.30
                    elif bias > 0.20:
                        result["cvd_signal"] = "BUY_MOMENTUM"
                        result["cvd_score"]  = 0.15
                    elif bias < -0.20:
                        result["cvd_signal"] = "SELL_MOMENTUM"
                        result["cvd_score"]  = -0.15
                    else:
                        result["cvd_signal"] = "BALANCED"
                        result["cvd_score"]  = 0.0

                    result["ok"]     = True
                    result["source"] = "bybit_cvd"
    except Exception:
        pass

    return result


async def fetch_cme_gaps(session: aiohttp.ClientSession, btc_price: float) -> Dict:
    """
    CME Bitcoin Futures Gaps — aimants de prix institutionnels.
    Logique : CME ferme le vendredi 22h UTC, rouvre lundi 23h UTC.
    Un gap = zone de prix non tradée = aimant → le marché y revient dans 70-80% des cas.
    Données : Yahoo Finance BTC futures (BTC=F) + calcul des gaps.
    """
    result = {
        "gap_up":        None,   # Prix du gap au-dessus
        "gap_down":      None,   # Prix du gap en-dessous
        "nearest_gap":   None,   # Gap le plus proche
        "gap_distance":  None,   # Distance en %
        "gap_signal":    "NO_GAP",
        "gap_score":     0.0,
        "ok": False, "source": "fallback"
    }
    try:
        # BTC CME futures sur Yahoo Finance
        url = "https://query1.finance.yahoo.com/v8/finance/chart/BTC=F?range=10d&interval=1d"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8),
                               headers=H_BROWSER) as r:
            if r.status == 200:
                d    = await r.json(content_type=None)
                res0 = d["chart"]["result"][0]
                ts   = res0.get("timestamp", [])
                q    = res0["indicators"]["quote"][0]
                opens  = q.get("open",  [])
                closes = q.get("close", [])

                if len(opens) >= 3 and len(closes) >= 3:
                    gaps_up   = []
                    gaps_down = []
                    for i in range(1, len(opens)):
                        prev_close = closes[i-1]
                        curr_open  = opens[i]
                        if prev_close and curr_open:
                            # Gap UP : open > close précédent
                            if curr_open > prev_close * 1.001:
                                gaps_up.append({"low": prev_close, "high": curr_open,
                                                "mid": (prev_close + curr_open) / 2})
                            # Gap DOWN : open < close précédent
                            elif curr_open < prev_close * 0.999:
                                gaps_down.append({"high": prev_close, "low": curr_open,
                                                  "mid": (prev_close + curr_open) / 2})

                    # Trouver le gap le plus proche du prix actuel
                    all_gaps = gaps_up + gaps_down
                    if all_gaps and btc_price > 0:
                        nearest = min(all_gaps, key=lambda g: abs(g["mid"] - btc_price))
                        dist_pct = (nearest["mid"] - btc_price) / btc_price * 100
                        result["nearest_gap"]  = round(nearest["mid"], 0)
                        result["gap_distance"] = round(dist_pct, 2)
                        # Gaps > 3% → moins pertinents pour court terme
                        if abs(dist_pct) < 5.0:
                            result["gap_signal"] = "GAP_UP_TARGET" if dist_pct > 0 else "GAP_DOWN_TARGET"
                            result["gap_score"]  = 0.15 if dist_pct > 0 else -0.15
                        if gaps_up:
                            result["gap_up"]   = round(gaps_up[-1]["mid"], 0)
                        if gaps_down:
                            result["gap_down"] = round(gaps_down[-1]["mid"], 0)

                    result["ok"]     = True
                    result["source"] = "yahoo_cme"
    except Exception:
        pass
    return result


async def fetch_deribit_options(session: aiohttp.ClientSession) -> Dict:
    """
    Deribit BTC Options public — Gamma exposure approximatif.
    Sans clé API. Endpoint public : /api/v2/public/get_book_summary_by_currency
    Signal respiration :
      Put/Call ratio > 1.2 → marché se hedge → peur → respiration BUY possible (contrarian)
      Put/Call ratio < 0.6 → euphorie → pas de hedge → respiration SELL possible
      Max Pain proche prix → cours magnétisé → range, pas de respiration
    """
    result = {
        "put_call_ratio": 1.0,
        "max_pain":       0.0,
        "iv_skew":        0.0,    # IV puts - IV calls (>0 = peur, <0 = euphorie)
        "options_signal": "NEUTRAL",
        "options_score":  0.0,
        "ok": False, "source": "fallback"
    }
    try:
        url = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10),
                               headers=H_BROWSER) as r:
            if r.status == 200:
                d    = await r.json(content_type=None)
                opts = d.get("result", [])
                if opts:
                    put_vol  = sum(float(o.get("volume", 0)) for o in opts if "-P" in o.get("instrument_name", ""))
                    call_vol = sum(float(o.get("volume", 0)) for o in opts if "-C" in o.get("instrument_name", ""))
                    if call_vol > 0:
                        pcr = put_vol / call_vol
                        result["put_call_ratio"] = round(pcr, 2)
                        # IV skew : moyenne IV puts vs calls
                        put_iv  = [float(o.get("mark_iv", 0)) for o in opts if "-P" in o.get("instrument_name","") and o.get("mark_iv")]
                        call_iv = [float(o.get("mark_iv", 0)) for o in opts if "-C" in o.get("instrument_name","") and o.get("mark_iv")]
                        if put_iv and call_iv:
                            result["iv_skew"] = round(sum(put_iv)/len(put_iv) - sum(call_iv)/len(call_iv), 1)
                    # Signal
                    pcr = result["put_call_ratio"]
                    sk  = result["iv_skew"]
                    score = 0.0
                    if pcr > 1.3:  score =  0.20   # peur excessive → contrarian BUY
                    elif pcr < 0.6: score = -0.20  # euphorie → contrarian SELL
                    if sk > 5:     score +=  0.10  # IV puts cher → peur
                    elif sk < -5:  score -=  0.10  # IV calls cher → euphorie
                    result["options_score"]  = round(max(-0.40, min(0.40, score)), 3)
                    result["options_signal"] = (
                        "FEAR_HEDGE"  if pcr > 1.3 else
                        "GREED_UNHEDGED" if pcr < 0.6 else
                        "NEUTRAL"
                    )
                    result["ok"]     = True
                    result["source"] = "deribit_public"
    except Exception:
        pass
    return result


def compute_breath_composite(funding: Dict, oi: Dict, cvd: Dict,
                              cme: Dict, options: Dict) -> Dict:
    """
    Agrège tous les signaux de respiration en un score composite unique.
    Retourne le signal final + score + raisons lisibles.
    Poids calibrés par fiabilité :
      CVD divergence    : 35% (signal le plus immédiat)
      Funding/LS        : 30% (pressure institutionnelle)
      OI signal         : 15% (contexte levier)
      Options PCR       : 12% (sentiment institutionnel)
      CME gap           : 8%  (aimant moyen terme)
    """
    scores = {
        "cvd":     cvd.get("cvd_score",      0.0) * 0.35,
        "funding": funding.get("breath_score", 0.0) * 0.30,
        "oi":      oi.get("oi_score",         0.0) * 0.15,
        "options": options.get("options_score", 0.0) * 0.12,
        "cme":     cme.get("gap_score",       0.0) * 0.08,
    }
    total = sum(scores.values())

    reasons = []
    if funding.get("breath_signal") != "NEUTRAL":
        f = funding.get("btc_funding", 0)
        ls = funding.get("btc_ls_long_pct", 50)
        reasons.append(f"Funding={f*100:.4f}% L/S_retail={ls:.0f}% → {funding['breath_signal']}")
    if oi.get("oi_signal") not in ("NEUTRAL",):
        reasons.append(f"OI_1h={oi.get('btc_oi_chg_1h',0):+.1f}% → {oi['oi_signal']}")
    if cvd.get("btc_divergence"):
        reasons.append(f"CVD_DIVERGENCE bias={cvd.get('btc_cvd_bias',0):+.2f} → {cvd['cvd_signal']}")
    if cme.get("gap_signal") != "NO_GAP":
        reasons.append(f"CME_GAP nearest={cme.get('nearest_gap')} dist={cme.get('gap_distance',0):+.1f}% → {cme['gap_signal']}")
    if options.get("options_signal") != "NEUTRAL":
        reasons.append(f"PCR={options.get('put_call_ratio',1):.2f} IV_skew={options.get('iv_skew',0):+.1f} → {options['options_signal']}")

    signal = (
        "SELL_BREATH" if total < -0.12 else
        "BUY_BREATH"  if total >  0.12 else
        "NEUTRAL"
    )
    intensity = (
        "STRONG"   if abs(total) > 0.25 else
        "MODERATE" if abs(total) > 0.12 else
        "WEAK"
    )

    return {
        "breath_composite_score": round(total, 3),
        "breath_signal":          signal,
        "breath_intensity":       intensity,
        "breath_scores":          {k: round(v, 3) for k, v in scores.items()},
        "breath_reasons":         reasons,
        "funding":                funding,
        "open_interest":          oi,
        "cvd":                    cvd,
        "cme_gaps":               cme,
        "options":                options,
        "ok": any([funding.get("ok"), oi.get("ok"), cvd.get("ok")]),
    }



# ================================================================================
# [WS-NEW-1] WYCKOFF PHASE DETECTOR — côté serveur, tous symboles
# ================================================================================

TTL_WYCKOFF  = 600   # 10 minutes — change lentement

async def fetch_wyckoff_phases(session: aiohttp.ClientSession,
                                symbols_ohlc: Dict[str, list]) -> Dict:
    """
    Détecte les phases Wyckoff (Accumulation/Distribution/Spring/Upthrust)
    sur les données OHLC D1 Yahoo Finance déjà en cache.
    Input : dict sym -> liste de closes/highs/lows
    Output : dict sym -> {phase, spring, confidence}
    """
    result = {}
    for sym, candles in symbols_ohlc.items():
        if len(candles) < 30:
            result[sym] = {"phase": "UNKNOWN", "spring": False, "confidence": 0.0}
            continue

        closes = [c["close"] for c in candles[-30:]]
        highs  = [c["high"]  for c in candles[-30:]]
        lows   = [c["low"]   for c in candles[-30:]]

        # ATR proxy
        atr = sum(highs[i]-lows[i] for i in range(len(closes))) / len(closes)
        if atr <= 0:
            result[sym] = {"phase": "UNKNOWN", "spring": False, "confidence": 0.0}
            continue

        # Range sur 15 dernières bougies
        rh = max(highs[-15:]); rl = min(lows[-15:])
        range_size = rh - rl

        # Range serré < 4× ATR
        if range_size > atr * 4.0:
            result[sym] = {"phase": "TRENDING", "spring": False, "confidence": 0.0}
            continue

        price_now = closes[-1]; price_30 = closes[0]
        preceded_drop = price_30 > price_now + atr * 3
        preceded_rise = price_30 < price_now - atr * 3

        # Mèches dominantes
        lw = sum(min(c["open"] if "open" in c else closes[i], closes[i]) - lows[i]
                 for i,c in enumerate(candles[-15:]))
        uw = sum(highs[i] - max(c["open"] if "open" in c else closes[i], closes[i])
                 for i,c in enumerate(candles[-15:]))

        lower_dom = lw > uw * 1.4
        upper_dom = uw > lw * 1.4

        phase = "RANGING"
        conf  = 0.5
        spring = False

        if preceded_drop and lower_dom:
            phase = "ACCUMULATION"; conf = 0.65
            # Spring : dernière bougie casse sous rl puis remonte
            if lows[-1] < rl - atr*0.1 and closes[-1] > rl:
                spring = True; conf = 0.80
        elif preceded_rise and upper_dom:
            phase = "DISTRIBUTION"; conf = 0.65
            # Upthrust : dernière bougie casse au-dessus rh puis revient
            if highs[-1] > rh + atr*0.1 and closes[-1] < rh:
                spring = True; conf = 0.80

        result[sym] = {
            "phase":      phase,
            "spring":     spring,
            "confidence": round(conf, 2),
            "range_high": round(rh, 5),
            "range_low":  round(rl, 5),
            "atr":        round(atr, 5),
        }
    return result


# ================================================================================
# [WS-NEW-2] CORRÉLATIONS GLISSANTES INTER-ACTIFS (20 jours)
# ================================================================================

TTL_CORREL = 1800  # 30 minutes

async def fetch_rolling_correlations(session: aiohttp.ClientSession) -> Dict:
    """
    Calcule les corrélations glissantes 20j entre BTC, SP500, Gold, DXY.
    BTC/SP500 > 0.70 → crypto suit actions (risk-on/off fort)
    BTC/Gold  > 0.50 → hedge flow
    BTC/DXY   < -0.60 → dollar fort = BTC baisse
    """
    symbols = {
        "btc":   "BTC-USD",
        "sp500": "^GSPC",
        "gold":  "GC=F",
        "dxy":   "DX-Y.NYB",
        "vix":   "^VIX",
        "nas":   "^IXIC",
    }
    closes_map = {}
    for key, yf_sym in symbols.items():
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_sym}?range=30d&interval=1d"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8),
                                   headers=H_BOT) as r:
                if r.status == 200:
                    d = await r.json(content_type=None)
                    q = d["chart"]["result"][0]["indicators"]["quote"][0]
                    cls = [x for x in q.get("close",[]) if x]
                    if len(cls) >= 20:
                        closes_map[key] = cls[-20:]
        except Exception:
            pass

    if len(closes_map) < 2:
        return {"ok": False, "correlations": {}}

    def pearson(a, b):
        n = min(len(a), len(b))
        if n < 5: return 0.0
        a, b = a[-n:], b[-n:]
        ma = sum(a)/n; mb = sum(b)/n
        num = sum((a[i]-ma)*(b[i]-mb) for i in range(n))
        da  = (sum((x-ma)**2 for x in a))**0.5
        db  = (sum((x-mb)**2 for x in b))**0.5
        return round(num/(da*db+1e-9), 3) if da*db > 0 else 0.0

    pairs = [
        ("btc",   "sp500"), ("btc",  "gold"),  ("btc",   "dxy"),
        ("btc",   "nas"),   ("gold", "dxy"),    ("sp500", "vix"),
    ]
    correls = {}
    for a, b in pairs:
        if a in closes_map and b in closes_map:
            correls[f"{a}_{b}"] = pearson(closes_map[a], closes_map[b])

    # Interprétation
    signals = []
    btc_sp = correls.get("btc_sp500", 0)
    btc_dxy= correls.get("btc_dxy",   0)
    btc_g  = correls.get("btc_gold",  0)
    sp_vix = correls.get("sp500_vix", 0)

    if btc_sp > 0.70:  signals.append(f"BTC_RISK_ON_FORT corr={btc_sp:.2f}")
    if btc_sp < 0.30:  signals.append(f"BTC_DECORRELE_SP500 corr={btc_sp:.2f}")
    if btc_dxy < -0.55: signals.append(f"DXY_PRESSE_BTC corr={btc_dxy:.2f}")
    if btc_g > 0.50:   signals.append(f"BTC_GOLD_HEDGE_FLOW corr={btc_g:.2f}")
    if sp_vix < -0.60: signals.append(f"VIX_SPIKE_RISK corr={sp_vix:.2f}")

    return {
        "ok":           True,
        "correlations": correls,
        "signals":      signals,
        "btc_regime":   "RISK_ASSET" if btc_sp > 0.60 else
                        "HEDGE"      if btc_g  > 0.50 else
                        "INDEPENDENT",
        "dxy_pressure": btc_dxy < -0.50,
    }


# ================================================================================
# [WS-NEW-3] LIQUIDATIONS FUTURES TEMPS RÉEL (Bybit public)
# ================================================================================

TTL_LIQUIDATIONS = 30   # 30 secondes — données très fraîches

async def fetch_liquidations(session: aiohttp.ClientSession) -> Dict:
    """
    Liquidations BTC futures (Bybit public — pas de clé).
    Cascade liquidations LONG  → flush → potentielle respiration BUY après
    Cascade liquidations SHORT → squeeze → potentielle respiration SELL après
    """
    result = {
        "btc_liq_long_usd":  0.0,
        "btc_liq_short_usd": 0.0,
        "liq_signal":        "NEUTRAL",
        "liq_cascade":       False,
        "ok": False, "source": "fallback"
    }
    try:
        # Bybit V5 insurance fund (proxy liquidations)
        url = "https://api.bybit.com/v5/market/insurance?coin=USDT"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=6),
                               headers=H_BOT) as r:
            if r.status == 200:
                d = await r.json(content_type=None)
                fund = d.get("result",{}).get("list",[])
                if fund:
                    # Fund qui baisse = beaucoup de liquidations absorbées
                    result["ok"]     = True
                    result["source"] = "bybit_insurance"
    except Exception:
        pass

    # Source 2 : Bybit recent trades avec large size = proxy liquidations
    try:
        url2 = "https://api.bybit.com/v5/market/recent-trade?category=linear&symbol=BTCUSDT&limit=50"
        async with session.get(url2, timeout=aiohttp.ClientTimeout(total=6),
                               headers=H_BOT) as r:
            if r.status == 200:
                d = await r.json(content_type=None)
                trades = d.get("result",{}).get("list",[])
                if trades:
                    # Trades de taille > 5 BTC = potentielles liquidations
                    big_buy  = sum(float(t.get("size",0)) for t in trades
                                   if t.get("side")=="Buy"  and float(t.get("size",0))>5)
                    big_sell = sum(float(t.get("size",0)) for t in trades
                                   if t.get("side")=="Sell" and float(t.get("size",0))>5)
                    result["btc_liq_long_usd"]  = round(big_sell * 62000, 0)  # SELL gros = liq longs
                    result["btc_liq_short_usd"] = round(big_buy  * 62000, 0)  # BUY gros = liq shorts

                    total_liq = result["btc_liq_long_usd"] + result["btc_liq_short_usd"]
                    cascade = total_liq > 5_000_000  # > 5M$ = cascade
                    result["liq_cascade"] = cascade

                    if result["btc_liq_long_usd"] > result["btc_liq_short_usd"] * 2:
                        result["liq_signal"] = "LONG_FLUSH"    # longs liquidés → rebond possible
                    elif result["btc_liq_short_usd"] > result["btc_liq_long_usd"] * 2:
                        result["liq_signal"] = "SHORT_SQUEEZE"  # shorts liquidés → chute possible
                    else:
                        result["liq_signal"] = "BALANCED"

                    result["ok"]     = True
                    result["source"] = "bybit_trades"
    except Exception:
        pass

    return result


# ================================================================================
# [WS-NEW-4] ORDER FLOW IMBALANCE — Coinbase Level 2 analysé
# ================================================================================

TTL_OFI = 30   # 30 secondes

async def fetch_order_flow_imbalance(session: aiohttp.ClientSession) -> Dict:
    """
    Déséquilibre bid/ask sur les 5 premiers niveaux Coinbase Level 2.
    OFI = (sum_bids - sum_asks) / (sum_bids + sum_asks)
    OFI > +0.30 = pression acheteuse forte
    OFI < -0.30 = pression vendeuse forte
    """
    result = {
        "btc_ofi":     0.0,
        "eth_ofi":     0.0,
        "bid_usd":     0.0,
        "ask_usd":     0.0,
        "ofi_signal":  "NEUTRAL",
        "ok": False, "source": "fallback"
    }
    for sym_cb, key in [("BTC-USD","btc"), ("ETH-USD","eth")]:
        try:
            url = f"https://api.exchange.coinbase.com/products/{sym_cb}/book?level=2"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=6),
                                   headers=H_BROWSER) as r:
                if r.status == 200:
                    d = await r.json(content_type=None)
                    bids = d.get("bids",[])[:10]
                    asks = d.get("asks",[])[:10]

                    bid_vol = sum(float(b[1]) for b in bids if len(b)>=2)
                    ask_vol = sum(float(a[1]) for a in asks if len(a)>=2)
                    total   = bid_vol + ask_vol

                    if total > 0:
                        ofi = (bid_vol - ask_vol) / total
                        result[f"{key}_ofi"] = round(ofi, 3)
                        if key == "btc":
                            result["bid_usd"] = round(bid_vol * float(bids[0][0]) if bids else 0, 0)
                            result["ask_usd"] = round(ask_vol * float(asks[0][0]) if asks else 0, 0)
                        result["ok"]     = True
                        result["source"] = "coinbase_l2"
        except Exception:
            pass

    # Signal final sur BTC OFI
    ofi = result["btc_ofi"]
    if   ofi >  0.35: result["ofi_signal"] = "STRONG_BUY_PRESSURE"
    elif ofi >  0.15: result["ofi_signal"] = "BUY_PRESSURE"
    elif ofi < -0.35: result["ofi_signal"] = "STRONG_SELL_PRESSURE"
    elif ofi < -0.15: result["ofi_signal"] = "SELL_PRESSURE"
    else:             result["ofi_signal"] = "BALANCED"

    return result


# ================================================================================
# [WS-NEW-4b] LIQUIDITY VOID DETECTOR — BTC, basé sur Coinbase L2 + OFI
#   Idée : un mouvement de prix violent peut être soit une vraie conviction
#   institutionnelle, soit juste un "vide" (peu d'ordres en face = book mince).
#   On mesure le ratio mouvement_prix / profondeur_traversée :
#     - ratio élevé = peu de volume a suffi à bouger le prix -> "vide"
#     - on croise avec l'OFI : si le vide est confirmé par un déséquilibre
#       OFI fort ET persistant dans le même sens -> VOID_CONFIRMED (réel)
#       sinon -> VOID_FAKE (faux signal, à ignorer côté EA)
# ================================================================================

TTL_LVD = 30  # secondes

async def fetch_liquidity_void(session: aiohttp.ClientSession,
                                ofi_result: Dict) -> Dict:
    """
    Auto-suffisant : récupère ses propres clôtures BTC récentes (1min Coinbase)
    pour mesurer le mouvement de prix, croisé avec ofi_result (OFI Coinbase L2).
    """
    result = {
        "void_ratio": 0.0,
        "void_detected": False,
        "void_status": "NONE",   # NONE | VOID_CONFIRMED | VOID_FAKE
        "ok": False, "source": "fallback"
    }
    try:
        # Clôtures BTC 1min (2 dernières) via Coinbase candles
        candles_url = "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60"
        async with session.get(candles_url, timeout=aiohttp.ClientTimeout(total=6),
                               headers=H_BROWSER) as r:
            if r.status != 200:
                return result
            candles = await r.json(content_type=None)
        # format coinbase: [time, low, high, open, close, volume], plus récent en premier
        if not isinstance(candles, list) or len(candles) < 2:
            return result
        recent_closes = [float(candles[1][4]), float(candles[0][4])]  # [avant, actuel]
        price_move = abs(recent_closes[-1] - recent_closes[-2])
        if price_move <= 0:
            return result

        url = "https://api.exchange.coinbase.com/products/BTC-USD/book?level=2"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=6),
                               headers=H_BROWSER) as r:
            if r.status != 200:
                return result
            d = await r.json(content_type=None)
            bids = d.get("bids", [])[:50]
            asks = d.get("asks", [])[:50]

        ref_price = recent_closes[-1]
        # Profondeur "traversée" : volume cumulé des niveaux compris entre
        # le prix de référence et prix_ref ± mouvement, des deux côtés.
        depth_crossed = 0.0
        for side in (bids, asks):
            for lvl in side:
                if len(lvl) < 2:
                    continue
                px, vol = float(lvl[0]), float(lvl[1])
                if abs(px - ref_price) <= price_move:
                    depth_crossed += vol * px  # en USD

        if depth_crossed <= 0:
            return result

        # Ratio : $ de mouvement de prix pour 1$ de profondeur traversée.
        ratio = price_move / depth_crossed * 1e6  # échelle arbitraire stable
        result["void_ratio"] = round(ratio, 4)
        result["ok"] = True
        result["source"] = "coinbase_l2"

        VOID_RATIO_THRESHOLD = 5.0  # à calibrer en shadow mode
        if ratio > VOID_RATIO_THRESHOLD:
            result["void_detected"] = True
            # Confirmation : OFI fort (>0.30 abs) dans le même sens que le mouvement
            ofi = ofi_result.get("btc_ofi", 0.0)
            move_dir = 1 if recent_closes[-1] > recent_closes[-2] else -1
            ofi_dir  = 1 if ofi > 0 else (-1 if ofi < 0 else 0)
            if abs(ofi) >= 0.30 and ofi_dir == move_dir:
                result["void_status"] = "VOID_CONFIRMED"  # vide + flux réel = signal valide
            else:
                result["void_status"] = "VOID_FAKE"       # vide sans flux = ignorer
        else:
            result["void_status"] = "NONE"

    except Exception:
        pass

    return result


# ================================================================================
# [WS-NEW-5] SEASONALITY BTC — patterns mensuels statistiques
# ================================================================================

# Données historiques BTC : win rate mensuel (2015-2025, 10 ans)
BTC_MONTHLY_STATS = {
    1:  {"wr": 0.78, "avg_ret": +12.5, "label": "JAN_BULL"},    # Janvier fort
    2:  {"wr": 0.64, "avg_ret":  +6.2, "label": "FEB_BULL"},
    3:  {"wr": 0.55, "avg_ret":  +3.1, "label": "MAR_NEUTRAL"},
    4:  {"wr": 0.64, "avg_ret":  +7.8, "label": "APR_BULL"},
    5:  {"wr": 0.55, "avg_ret":  +1.2, "label": "MAY_NEUTRAL"},
    6:  {"wr": 0.45, "avg_ret":  -2.4, "label": "JUN_WEAK"},    # Juin faible
    7:  {"wr": 0.64, "avg_ret":  +8.3, "label": "JUL_BULL"},
    8:  {"wr": 0.45, "avg_ret":  -1.8, "label": "AUG_WEAK"},
    9:  {"wr": 0.18, "avg_ret": -11.2, "label": "SEP_BEAR"},    # Septembre = pire mois
    10: {"wr": 0.73, "avg_ret": +17.4, "label": "OCT_BULL"},    # Uptober
    11: {"wr": 0.73, "avg_ret": +15.2, "label": "NOV_BULL"},
    12: {"wr": 0.64, "avg_ret":  +6.8, "label": "DEC_BULL"},
}

# Jours de la semaine (0=lundi)
# Sources : ScienceDirect (2013-2021 EGARCH), AristoQuant BTCUSD analysis, QuantifiedStrategies backtest
# NOTE comportementale : lundi/mercredi = forte volatilité + trend ; vendredi = CME expiry complexe ;
#      samedi = volume bas → prix plus manipulable (pas forcément buy) ; dimanche soir = zone achat
BTC_WEEKDAY_STATS = {
    0: {"wr": 0.56, "vol_regime": "HIGH",   "label": "MON_BULL",        "note": "Retour liquidité EU/US, gap weekend comblé, momentum fort"},
    1: {"wr": 0.55, "vol_regime": "HIGH",   "label": "TUE_BULL",        "note": "Continuation lundi, volatilité élevée, institutional flow"},
    2: {"wr": 0.54, "vol_regime": "HIGH",   "label": "WED_SLIGHT_BULL", "note": "Milieu semaine, bulls dominants sur données 2018-2021"},
    3: {"wr": 0.49, "vol_regime": "HIGH",   "label": "THU_NEUTRAL",     "note": "Volatilité forte (CME weekly expiry mon/wed/fri), direction mixte"},
    4: {"wr": 0.50, "vol_regime": "MEDIUM", "label": "FRI_COMPLEX",     "note": "CME mensuel last-friday 4pm London (16hUTC) = pin risk ; 14h-16h UTC = volatilité pic"},
    5: {"wr": 0.51, "vol_regime": "LOW",    "label": "SAT_LOW_VOL",     "note": "Volume bas (-40% vs weekday), prix manipulable, pas de flow institutionnel"},
    6: {"wr": 0.52, "vol_regime": "LOW",    "label": "SUN_BUY_ZONE",    "note": "18h-23h UTC = meilleur prix d'achat semaine (avg -2.3% vs peak), transition bull"},
}

# ── PROFIL HORAIRE UTC BTC ────────────────────────────────────────────────────
# Sources académiques consolidées :
#   - Gemini 2015-2023 (Quantpedia/Padysak & Vojtko) : H22-H23 UTC = retours les plus élevés
#   - ScienceDirect 2024 (50k minutes Binance) : volume peak H14-H16, vol RV minimum H05
#   - Bitstamp/Ledger Journal : pattern reverse-V, peak European/US overlap
#   - AmberData 2025 (50526min orderbook) : 21h UTC = danger zone liquidité, 09-14h = optimal
#   - PocketOption (5yr): 03h-04h UTC = prix -1.7% vs journée, meilleure entrée asiatique
#
# Structure documentée du profil BTC/24h (UTC) :
#   H00-H02 : TRANSITION — décharge fin NY, volume minimal, tendance légère post-close NYSE
#   H01     : PIC POSITIF — ouverture Hang Seng (ScienceDirect confirmé statistiquement)
#   H02-H05 : DEAD ZONE — minimum absolu volume et volatilité (RV minimum = H05)
#   H05     : MICRO-PIC — 2e partie Hang Seng, positif mais faible amplitude
#   H07-H09 : RÉVEIL EUROPE — liquidité croissante, biais légèrement positif pré-London
#   H09-H14 : PHASE EUROPÉENNE — volume croissant, depth orderbook optimal (3.61M$ à H11)
#   H14-H16 : OVERLAP LN/NY = PIC VOLUME — highest volume day + volatilité max (RV peak H14)
#             Vendredi H14-H16 = plus grands mouvements positifs de la semaine (Interdax 2017-2020)
#   H16-H20 : SESSION NY — retours proches de 0 ou négatifs (NYSE trading hours)
#   H20     : MICRO-PIC — NYSE close, retour positif BTC documenté (ScienceDirect)
#   H21     : DANGER ZONE — fermeture desks institutionnels, retrait liquidité systématique
#             (AmberData: trough liquidity $2.36M vs $4.43M weekend peak)
#   H21-H23 : FENÊTRE BULL NOCTURNE — retours H22-H23 = les plus élevés de la journée
#             (Quantpedia/Padysak & Vojtko, confirmé sur 8 ans de données Gemini)
#             Stratégie BUY H21 → EXIT H23 : 33% annualisé, drawdown -22% (vs -70% hold)
#
# Format : bias (-1.0 à +1.0), vol_mult (multiplicateur volatilité normalisé),
#          wr_buy (win rate direction BUY), regime (qualité du marché)
BTC_HOURLY_STATS = {
    # heure : {bias, vol_mult, wr_buy, regime, note}
    0:  {"bias": +0.030, "vol_mult": 0.72, "wr_buy": 0.515, "regime": "TRANSITION",   "note": "Post-NYSE close, volume décroissant"},
    1:  {"bias": +0.080, "vol_mult": 0.75, "wr_buy": 0.540, "regime": "MICRO_BULL",   "note": "Ouverture Hang Seng — retour positif statistiquement significatif"},
    2:  {"bias": -0.020, "vol_mult": 0.55, "wr_buy": 0.495, "regime": "DEAD_ZONE",    "note": "Zone morte — spread élargi, liquidité minimale"},
    3:  {"bias": -0.040, "vol_mult": 0.50, "wr_buy": 0.487, "regime": "DEAD_ZONE",    "note": "Creux absolu journalier — prix -1.7% vs moyenne (meilleure entrée long terme)"},
    4:  {"bias": -0.030, "vol_mult": 0.50, "wr_buy": 0.490, "regime": "DEAD_ZONE",    "note": "Suite zone morte asiatique"},
    5:  {"bias": +0.050, "vol_mult": 0.58, "wr_buy": 0.525, "regime": "MICRO_BULL",   "note": "2e partie Hang Seng — micro pic positif"},
    6:  {"bias": +0.010, "vol_mult": 0.62, "wr_buy": 0.505, "regime": "LOW_VOL",      "note": "Transition Asie→Europe, volume encore faible"},
    7:  {"bias": +0.025, "vol_mult": 0.70, "wr_buy": 0.512, "regime": "PRE_LONDON",   "note": "Réveil Europe, biais positif naissant"},
    8:  {"bias": +0.040, "vol_mult": 0.80, "wr_buy": 0.520, "regime": "PRE_LONDON",   "note": "Préouverture London, momentum EU"},
    9:  {"bias": +0.055, "vol_mult": 0.90, "wr_buy": 0.528, "regime": "LONDON",       "note": "Session London active, depth orderbook croissant"},
    10: {"bias": +0.065, "vol_mult": 0.95, "wr_buy": 0.532, "regime": "LONDON",       "note": "London mid — biais haussier modéré"},
    11: {"bias": +0.070, "vol_mult": 1.00, "wr_buy": 0.535, "regime": "LONDON_PEAK",  "note": "Depth max orderbook ($3.86M à H11 — AmberData 2025)"},
    12: {"bias": +0.060, "vol_mult": 1.05, "wr_buy": 0.530, "regime": "LONDON",       "note": "London fin, pré-overlap NY"},
    13: {"bias": +0.055, "vol_mult": 1.10, "wr_buy": 0.528, "regime": "PRE_OVERLAP",  "note": "Pré-ouverture NYSE, tension croissante"},
    14: {"bias": +0.020, "vol_mult": 1.35, "wr_buy": 0.510, "regime": "OVERLAP_PEAK", "note": "VOLUME MAX + VOLATILITÉ MAX (RV peak H14) — mouvements amples dans les 2 sens"},
    15: {"bias": +0.015, "vol_mult": 1.30, "wr_buy": 0.508, "regime": "OVERLAP_PEAK", "note": "Overlap LN/NY — volume institutionnel dominant, spreads comprimés"},
    16: {"bias": -0.010, "vol_mult": 1.20, "wr_buy": 0.495, "regime": "NEW_YORK",     "note": "NYSE actif — retours BTC proches 0 ou négatifs pendant NYSE (documenté)"},
    17: {"bias": -0.020, "vol_mult": 1.10, "wr_buy": 0.490, "regime": "NEW_YORK",     "note": "Session NY mid — retours BTC légèrement négatifs"},
    18: {"bias": -0.015, "vol_mult": 1.05, "wr_buy": 0.493, "regime": "NEW_YORK",     "note": "NY continuation — correction fréquente"},
    19: {"bias": -0.010, "vol_mult": 1.00, "wr_buy": 0.495, "regime": "NEW_YORK",     "note": "NY fin session, volume décroissant"},
    20: {"bias": +0.060, "vol_mult": 0.90, "wr_buy": 0.530, "regime": "NYSE_CLOSE",   "note": "Close NYSE — micro pic haussier BTC documenté statistiquement (ScienceDirect)"},
    21: {"bias": -0.030, "vol_mult": 0.75, "wr_buy": 0.485, "regime": "DANGER_ZONE",  "note": "DANGER : fermeture desks institutionnels, retrait liquidité systématique ($2.36M trough)"},
    22: {"bias": +0.120, "vol_mult": 0.80, "wr_buy": 0.580, "regime": "BULL_WINDOW",  "note": "MEILLEURE HEURE — retour le plus élevé de la journée (Gemini 2015-2023, Quantpedia)"},
    23: {"bias": +0.110, "vol_mult": 0.78, "wr_buy": 0.575, "regime": "BULL_WINDOW",  "note": "2e meilleure heure — marchés traditionnels tous fermés, crypto seul"},
}

def _is_cme_last_friday_expiry(today: "date") -> bool:
    """Retourne True si aujourd'hui est le dernier vendredi du mois (CME monthly expiry)."""
    import calendar
    if today.weekday() != 4:  # 4 = vendredi
        return False
    # Dernier vendredi = pas de vendredi dans les 7 prochains jours du même mois
    next_friday = today.day + 7
    _, last_day = calendar.monthrange(today.year, today.month)
    return next_friday > last_day

def _compute_post_halving_months() -> int:
    """Calcule dynamiquement les mois écoulés depuis le dernier halving BTC (avril 2024)."""
    from datetime import date
    # Halvings BTC : 2024-04-20 (block 840000), prochain ~2028-04
    LAST_HALVING = date(2024, 4, 20)
    today = date.today()
    delta = today - LAST_HALVING
    return int(delta.days / 30.44)  # mois approximatifs

def compute_btc_seasonality(hour_utc: int = -1) -> Dict:
    """
    Calcule le biais saisonnier BTC multi-couche pour date+heure courante.

    Couches intégrées (V121) :
      1. Mois de l'année (wr mensuel historique)
      2. Jour de la semaine (wr journalier + vol_regime)
      3. Heure UTC (bias horaire + vol_mult + regime de marché)
      4. Effet post-halving (dynamique, calculé automatiquement)
      5. Effets calendrier : fin de mois, CME last-friday expiry, weekend transition

    Pondération : mois 45% + jour 25% + heure 30%
    """
    from datetime import date, datetime, timezone
    today = date.today()
    month   = today.month
    weekday = today.weekday()
    day     = today.day

    if hour_utc < 0:
        hour_utc = datetime.now(timezone.utc).hour

    m_stat = BTC_MONTHLY_STATS.get(month,   {"wr": 0.50, "avg_ret": 0,    "label": "UNKNOWN"})
    w_stat = BTC_WEEKDAY_STATS.get(weekday,  {"wr": 0.50, "vol_regime": "MEDIUM", "label": "UNKNOWN", "note": ""})
    h_stat = BTC_HOURLY_STATS.get(hour_utc,  {"bias": 0.0, "vol_mult": 1.0, "wr_buy": 0.500, "regime": "UNKNOWN", "note": ""})

    # ── Score composite pondéré ────────────────────────────────────────────
    # Mois : tendance de fond (45%)
    # Jour  : biais structurel hebdo (25%)
    # Heure : biais intraday précis (30%)
    month_score   = (m_stat["wr"] - 0.50) * 2.0
    weekday_score = (w_stat["wr"] - 0.50) * 2.0
    hourly_score  = h_stat["bias"]  # déjà en -1.0 à +1.0

    score = month_score * 0.45 + weekday_score * 0.25 + hourly_score * 0.30

    # ── Post-halving (dynamique) ───────────────────────────────────────────
    post_halving_months = _compute_post_halving_months()
    if 6 <= post_halving_months <= 18:
        halving_bonus = +0.20   # phase bull post-halving
    elif 18 < post_halving_months <= 30:
        halving_bonus = -0.08   # correction progressive post-pic
    elif post_halving_months > 30:
        halving_bonus = -0.05   # accumulation pré-prochain halving
    else:
        halving_bonus = +0.05   # pré-halving (< 6 mois après)

    # ── Effets calendrier ─────────────────────────────────────────────────
    calendar_effects = []
    calendar_penalty = 0.0

    # Fin/début de mois (rebalancing portfolio)
    if day >= 28 or day <= 3:
        calendar_penalty -= 0.05
        calendar_effects.append("END_OF_MONTH_SELL")

    # CME last-friday monthly expiry (16h UTC = 4pm London)
    # Zone dangereuse H13-H17 ce jour-là : pin risk + stop hunt autour du strike max pain
    is_cme_expiry_day = _is_cme_last_friday_expiry(today)
    if is_cme_expiry_day:
        if 13 <= hour_utc <= 17:
            calendar_penalty -= 0.15  # forte pénalité = danger pin risk
            calendar_effects.append("CME_MONTHLY_EXPIRY_WINDOW")
        else:
            calendar_penalty -= 0.05
            calendar_effects.append("CME_MONTHLY_EXPIRY_DAY")

    # CME weekly expiry (lundi/mercredi/vendredi) — volatilité accrue H13-H17
    if weekday in (0, 2, 4) and 13 <= hour_utc <= 17:
        calendar_penalty -= 0.05
        calendar_effects.append("CME_WEEKLY_EXPIRY_WINDOW")

    # Zone de transition weekend→semaine (dimanche H18-H23 : buy zone documentée)
    if weekday == 6 and 18 <= hour_utc <= 23:
        calendar_penalty += 0.08  # bonus buy — prix en moyenne -2.3% vs peak
        calendar_effects.append("SUNDAY_BUY_ZONE")

    # Dead zone nocturne (H02-H05 UTC) : spread élargi, manipulation facile
    if 2 <= hour_utc <= 5:
        calendar_penalty -= 0.08
        calendar_effects.append("DEAD_ZONE_UTC")

    # Fenêtre bull nocturne (H22-H23 UTC) : meilleurs retours documentés sur 8 ans
    if hour_utc in (22, 23):
        calendar_penalty += 0.10
        calendar_effects.append("BULL_WINDOW_H22H23")

    # ── Score final ───────────────────────────────────────────────────────
    final_score = round(max(-1.0, min(1.0,
                        score + halving_bonus + calendar_penalty)), 3)

    # ── Régime de marché horaire ──────────────────────────────────────────
    hourly_regime = h_stat["regime"]
    vol_mult      = h_stat["vol_mult"]

    # Surcharge vol_regime si weekend (volume structurellement bas)
    if weekday in (5, 6):
        vol_mult = round(vol_mult * 0.70, 2)  # -30% volume weekend

    return {
        # Couche mensuelle
        "month_label":          m_stat["label"],
        "month_wr":             m_stat["wr"],
        "month_avg_ret":        m_stat["avg_ret"],
        # Couche journalière
        "weekday_label":        w_stat["label"],
        "weekday_wr":           w_stat["wr"],
        "weekday_vol_regime":   w_stat["vol_regime"],
        # Couche horaire
        "hour_utc":             hour_utc,
        "hourly_regime":        hourly_regime,
        "hourly_bias":          h_stat["bias"],
        "hourly_wr_buy":        h_stat["wr_buy"],
        "hourly_vol_mult":      vol_mult,
        "hourly_note":          h_stat["note"],
        # Post-halving
        "halving_bonus":        round(halving_bonus, 2),
        "post_halving_months":  post_halving_months,
        # Effets calendrier actifs
        "calendar_effects":     calendar_effects,
        "calendar_penalty":     round(calendar_penalty, 3),
        "is_cme_expiry_day":    is_cme_expiry_day,
        # Score final
        "seasonality_score":    final_score,
        "signal":               "SEASONAL_BULL"    if final_score > 0.15  else
                                "SEASONAL_BEAR"    if final_score < -0.15 else
                                "SEASONAL_NEUTRAL",
    }


# ================================================================================
# [WS-NEW-6] DIVERGENCE RSI/PRIX — détection sur closes + RSI calculé
# ================================================================================

def compute_rsi_divergence(closes: list, rsi_period: int = 14) -> Dict:
    """
    Détecte les divergences RSI/prix sur une série de closes.
    Divergence haussière : prix fait LL mais RSI fait HL → retournement haussier
    Divergence baissière : prix fait HH mais RSI fait LH → retournement baissier
    """
    if len(closes) < rsi_period + 10:
        return {"divergence": "NONE", "type": "NONE", "confidence": 0.0}

    # Calcul RSI série complète
    def calc_rsi_series(cls, period):
        gains = losses = []
        rsis = []
        for i in range(1, len(cls)):
            d = cls[i] - cls[i-1]
            gains.append(max(d, 0)); losses.append(max(-d, 0))
        if len(gains) < period: return []
        avg_g = sum(gains[:period])/period
        avg_l = sum(losses[:period])/period
        rsi_list = []
        for i in range(period, len(gains)):
            avg_g = (avg_g*(period-1)+gains[i])/period
            avg_l = (avg_l*(period-1)+losses[i])/period
            rs    = avg_g/(avg_l+1e-9)
            rsi_list.append(100 - 100/(1+rs))
        return rsi_list

    rsi_series = calc_rsi_series(closes, rsi_period)
    if len(rsi_series) < 8:
        return {"divergence": "NONE", "type": "NONE", "confidence": 0.0}

    # Comparer 2 derniers swings (récent vs précédent)
    n = len(rsi_series)
    price_recent = closes[-3:]
    price_prev   = closes[-8:-3]
    rsi_recent   = rsi_series[-3:]
    rsi_prev     = rsi_series[-8:-3]

    if not price_prev or not rsi_prev: 
        return {"divergence": "NONE", "type": "NONE", "confidence": 0.0}

    p_hi_r = max(price_recent); p_lo_r = min(price_recent)
    p_hi_p = max(price_prev);   p_lo_p = min(price_prev)
    r_hi_r = max(rsi_recent);   r_lo_r = min(rsi_recent)
    r_hi_p = max(rsi_prev);     r_lo_p = min(rsi_prev)

    div_type = "NONE"; confidence = 0.0

    # Divergence baissière : prix HH mais RSI LH
    if p_hi_r > p_hi_p * 1.002 and r_hi_r < r_hi_p - 2.0:
        div_type   = "BEARISH"
        confidence = min(1.0, (p_hi_r/p_hi_p - 1)*20 + (r_hi_p-r_hi_r)/20)

    # Divergence haussière : prix LL mais RSI HL
    elif p_lo_r < p_lo_p * 0.998 and r_lo_r > r_lo_p + 2.0:
        div_type   = "BULLISH"
        confidence = min(1.0, (1-p_lo_r/p_lo_p)*20 + (r_lo_r-r_lo_p)/20)

    # Divergence cachée haussière : prix HL + RSI LL (continuation haussière)
    elif p_lo_r > p_lo_p * 1.001 and r_lo_r < r_lo_p - 2.0:
        div_type   = "HIDDEN_BULLISH"
        confidence = 0.50

    # Divergence cachée baissière : prix LH + RSI HH (continuation baissière)
    elif p_hi_r < p_hi_p * 0.999 and r_hi_r > r_hi_p + 2.0:
        div_type   = "HIDDEN_BEARISH"
        confidence = 0.50

    return {
        "divergence": div_type,
        "type":       div_type,
        "confidence": round(min(1.0, confidence), 2),
        "rsi_recent": round(sum(rsi_recent)/len(rsi_recent), 1) if rsi_recent else 50.0,
        "rsi_prev":   round(sum(rsi_prev)/len(rsi_prev), 1) if rsi_prev else 50.0,
        "signal":     "BUY"  if div_type in ("BULLISH","HIDDEN_BULLISH") else
                      "SELL" if div_type in ("BEARISH","HIDDEN_BEARISH") else
                      "NEUTRAL",
    }


# ================================================================================
# [WS-NEW-7] INTÉGRATION DIVERGENCES DANS fetch_technicals_for_symbol
# ================================================================================

async def fetch_technicals_with_divergence(
    session: aiohttp.ClientSession,
    sym: str, profile: Dict,
    timeframes: List[str] = None
) -> Dict:
    """
    Extension de fetch_technicals_for_symbol avec divergences RSI.
    Ajoute le signal de divergence dans le score final.
    """
    base = await fetch_technicals_for_symbol(session, sym, profile, timeframes)
    if not base.get("ok"):
        return base

    # Récupérer les closes du TF journalier pour divergence
    yahoo_sym = YAHOO_SYMBOL_MAP.get(sym)
    if not yahoo_sym:
        return base

    divergence = {"divergence": "NONE", "signal": "NEUTRAL", "confidence": 0.0}
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}?range=60d&interval=1d"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8),
                               headers=H_BOT) as r:
            if r.status == 200:
                d = await r.json(content_type=None)
                q = d["chart"]["result"][0]["indicators"]["quote"][0]
                closes = [x for x in q.get("close",[]) if x]
                if len(closes) >= 25:
                    divergence = compute_rsi_divergence(closes)
    except Exception:
        pass

    # Intégrer divergence dans le score (poids 15%)
    div_score = 0.0
    if divergence["signal"] == "BUY":
        div_score =  divergence["confidence"] * 0.40
    elif divergence["signal"] == "SELL":
        div_score = -divergence["confidence"] * 0.40

    # Ajuster le score de base
    new_score = round(max(-1.0, min(1.0, base["score"] * 0.85 + div_score * 0.15)), 3)

    base["score"]      = new_score
    base["divergence"] = divergence
    if new_score >= 0.25:    base["signal"] = "BUY"
    elif new_score <= -0.25: base["signal"] = "SELL"
    else:                    base["signal"] = "NEUTRAL"

    return base




# [BOOST] SOURCE 1 — ON-CHAIN METRICS (Blockchain.info + Mempool.space)
# ============================================================================
async def fetch_onchain_metrics(session: aiohttp.ClientSession) -> Dict:
    result = {
        "hash_rate": 0.0, "mempool_size": 0, "mempool_fee": 0.0,
        "onchain_score": 0.0, "onchain_signal": "NEUTRAL",
        "ok": False, "source": "fallback"
    }
    score = 0.0
    try:
        async with session.get("https://blockchain.info/stats?format=json",
                               timeout=aiohttp.ClientTimeout(total=8), headers=H_BROWSER) as r:
            if r.status == 200:
                d = await r.json(content_type=None)
                result["hash_rate"] = float(d.get("hash_rate", 0) or 0)
                result["ok"] = True; result["source"] = "blockchain_info"
                if result["hash_rate"] > 0: score += 0.10
    except Exception: pass
    try:
        async with session.get("https://mempool.space/api/mempool",
                               timeout=aiohttp.ClientTimeout(total=6), headers=H_BROWSER) as r:
            if r.status == 200:
                d = await r.json(content_type=None)
                result["mempool_size"] = int(d.get("count", 0) or 0)
                result["ok"] = True
                if result["source"] == "fallback": result["source"] = "mempool"
                else: result["source"] += "+mempool"
                if result["mempool_size"] > 50000: score += 0.15
                elif result["mempool_size"] > 20000: score += 0.07
    except Exception: pass
    try:
        async with session.get("https://mempool.space/api/v1/fees/recommended",
                               timeout=aiohttp.ClientTimeout(total=5), headers=H_BROWSER) as r:
            if r.status == 200:
                d = await r.json(content_type=None)
                result["mempool_fee"] = float(d.get("halfHourFee", 0) or 0)
                if result["mempool_fee"] > 50: score += 0.10
                elif result["mempool_fee"] > 20: score += 0.05
    except Exception: pass
    result["onchain_score"] = round(max(-0.40, min(0.40, score)), 3)
    result["onchain_signal"] = ("ONCHAIN_BULL" if score > 0.15 else "ONCHAIN_BEAR" if score < -0.15 else "NEUTRAL")
    return result


# ============================================================================
# [BOOST] SOURCE 2 — NEWS SENTIMENT (NewsAPI + GNews + CryptoPanic fallback)
# ============================================================================
async def fetch_newsapi_sentiment(session: aiohttp.ClientSession) -> Dict:
    import os
    result = {
        "bull_headlines": 0, "bear_headlines": 0, "total": 0,
        "sentiment_pct": 50.0, "news_score": 0.0, "news_signal": "NEUTRAL",
        "top_titles": [], "ok": False, "source": "fallback"
    }
    BULL_KW = ["surge","rally","ath","bullish","buy","breakout","pump","soar","rise","gain","boost","record","recover","approve","etf"]
    BEAR_KW = ["crash","dump","sell","bearish","fall","drop","fear","ban","hack","regulation","lose","plunge","collapse","lawsuit","fraud"]
    articles = []
    key = os.environ.get("NEWSAPI_KEY", "")
    if key:
        try:
            url = (f"https://newsapi.org/v2/everything?q=bitcoin+OR+gold+OR+%22federal+reserve%22"
                   f"&language=en&sortBy=publishedAt&pageSize=20&apiKey={key}")
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 200:
                    d = await r.json(content_type=None)
                    articles = [a.get("title","") for a in d.get("articles",[]) if a.get("title")]
                    result["source"] = "newsapi"
        except Exception: pass
    if not articles:
        try:
            async with session.get("https://cryptopanic.com/api/free/v1/posts/?auth_token=free&filter=hot&currencies=BTC",
                                   timeout=aiohttp.ClientTimeout(total=7), headers=H_BROWSER) as r:
                if r.status == 200:
                    d = await r.json(content_type=None)
                    articles = [p.get("title","") for p in d.get("results",[]) if p.get("title")]
                    result["source"] = "cryptopanic"
        except Exception: pass
    if articles:
        bull = sum(1 for t in articles if any(kw in t.lower() for kw in BULL_KW))
        bear = sum(1 for t in articles if any(kw in t.lower() for kw in BEAR_KW))
        result.update({"bull_headlines": bull, "bear_headlines": bear, "total": len(articles),
                        "top_titles": [t[:70] for t in articles[:5]], "ok": True})
        pct = (bull / max(bull+bear,1)) * 100 if (bull+bear) > 0 else 50.0
        result["sentiment_pct"] = round(pct, 1)
        score = (0.20 if pct>70 else 0.10 if pct>60 else -0.20 if pct<30 else -0.10 if pct<40 else 0.0)
        result["news_score"] = round(max(-0.30, min(0.30, score)), 3)
        result["news_signal"] = ("NEWS_BULL" if score>0.08 else "NEWS_BEAR" if score<-0.08 else "NEUTRAL")
    return result


# ============================================================================
# [BOOST] SOURCE 3 — REDDIT SENTIMENT (r/CryptoCurrency + r/Bitcoin)
# ============================================================================
async def fetch_reddit_sentiment(session: aiohttp.ClientSession) -> Dict:
    result = {
        "reddit_bull_pct": 50.0, "reddit_score": 0.0,
        "reddit_signal": "NEUTRAL", "hot_count": 0,
        "ok": False, "source": "fallback"
    }
    BULL_KW = ["bull","moon","ath","buy","pump","long","hodl","accumulate","breakout","up"]
    BEAR_KW = ["bear","crash","dump","sell","short","drop","fear","recession","fall","down"]
    bull = bear = count = 0
    for sub in ["CryptoCurrency", "Bitcoin"]:
        try:
            async with session.get(f"https://www.reddit.com/r/{sub}/hot.json?limit=25",
                                   timeout=aiohttp.ClientTimeout(total=8),
                                   headers={"User-Agent": "STALINE_SCANNER/3.5 (research)"}) as r:
                if r.status == 200:
                    posts = (await r.json(content_type=None)).get("data",{}).get("children",[])
                    for p in posts:
                        pd_ = p.get("data",{})
                        title = (pd_.get("title","") or "").lower()
                        ratio = float(pd_.get("upvote_ratio", 0.5) or 0.5)
                        b = sum(1 for kw in BULL_KW if kw in title)
                        be = sum(1 for kw in BEAR_KW if kw in title)
                        if b > 0 or be > 0:
                            bull += b * ratio; bear += be * (1-ratio+0.5); count += 1
                    result["ok"] = True; result["source"] = "reddit"
        except Exception: continue
    if count > 0:
        pct = (bull / max(bull+bear, 0.01)) * 100
        result.update({"hot_count": count, "reddit_bull_pct": round(pct, 1)})
        score = (0.15 if pct>65 else 0.07 if pct>55 else -0.15 if pct<35 else -0.07 if pct<45 else 0.0)
        result["reddit_score"] = round(max(-0.25, min(0.25, score)), 3)
        result["reddit_signal"] = ("REDDIT_BULL" if score>0.08 else "REDDIT_BEAR" if score<-0.08 else "NEUTRAL")
    return result


# ============================================================================
# [BOOST] SOURCE 4 — ECONOMIC CALENDAR (ForexFactory RSS)
# Réduit l'exposition si event HIGH IMPACT dans <2h
# ============================================================================
async def fetch_econocalendar(session: aiohttp.ClientSession) -> Dict:
    import re as _re
    from datetime import timezone as _tz
    result = {
        "high_impact_upcoming": False, "next_event_name": "",
        "next_event_minutes": 999, "events_today": [],
        "calendar_score": 0.0, "calendar_signal": "CLEAR",
        "ok": False, "source": "fallback"
    }
    HIGH_KW = ["cpi","nfp","gdp","fomc","ppi","ism","jolts","payroll","interest rate",
               "fed","ecb","boe","inflation","retail sales","unemployment","durable"]
    now_utc = datetime.now(_tz.utc)
    try:
        async with session.get("https://www.forexfactory.com/ff_calendar_thisweek.xml",
                               timeout=aiohttp.ClientTimeout(total=8), headers=H_BROWSER) as r:
            if r.status == 200:
                text = await r.text()
                events = _re.findall(
                    r'<title>(.*?)</title>.*?<country>(.*?)</country>.*?<date>(.*?)</date>.*?<time>(.*?)</time>.*?<impact>(.*?)</impact>',
                    text, _re.DOTALL)
                high_events = []
                for ev in events:
                    name, country, date_s, time_s, impact = [x.strip() for x in ev]
                    if country.upper() not in ("USD","EUR","GBP"): continue
                    if impact.lower() not in ("high","medium"): continue
                    if any(kw in name.lower() for kw in HIGH_KW):
                        high_events.append({"name": name, "time": time_s, "impact": impact})
                result["events_today"] = high_events[:5]
                result["ok"] = True; result["source"] = "forexfactory"
                min_mins = 999
                next_name = ""
                for ev in high_events:
                    try:
                        t_str = f"{ev['time']} UTC"
                        # Parse "8:30am" style
                        t_ev = datetime.strptime(ev["time"], "%I:%M%p").replace(
                            year=now_utc.year, month=now_utc.month, day=now_utc.day, tzinfo=_tz.utc)
                        mins = (t_ev - now_utc).total_seconds() / 60
                        if 0 < mins < min_mins:
                            min_mins = int(mins); next_name = ev["name"]
                    except Exception: continue
                result["next_event_minutes"] = min_mins
                result["next_event_name"]    = next_name
                if 0 < min_mins < 120:
                    result["high_impact_upcoming"] = True
                    danger = max(0.0, 1.0 - min_mins / 120.0)
                    result["calendar_score"]  = round(-0.50 * danger, 3)
                    result["calendar_signal"] = ("DANGER_IMMINENT" if min_mins < 30 else "EVENT_SOON")
    except Exception: pass
    return result



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
        # [WS-NEW] Timestamps nouvelles sources
        self._last_wyckoff      = 0.0
        self._last_correl       = 0.0
        self._last_liquidations = 0.0
        self._last_ofi          = 0.0
        self._last_lvd          = 0.0
        # [GEO-ECO+] Timestamps avancés
        self._last_gdelt   = 0.0
        self._last_fomc    = 0.0
        self._last_pmi     = 0.0
        self._last_twitter = 0.0
        self._last_onchain  = 0.0
        self._last_newsapi  = 0.0
        self._last_reddit   = 0.0
        self._last_econocal = 0.0
        # [BREATH] Timestamps respirations
        self._last_breath_funding = 0.0
        self._last_breath_oi      = 0.0
        self._last_breath_cvd     = 0.0
        self._last_breath_cme     = 0.0
        self._last_breath_options = 0.0
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
                    # [V125-PRECISION] Utilise fetch_technicals_with_divergence
                    # (détection divergence RSI/prix) au lieu de fetch_technicals_for_symbol
                    # — nécessaire pour distinguer un vrai retournement d'une simple
                    # respiration/pullback dans la tendance de fond.
                    TTL_TECH = 120
                    tech_tasks = {}
                    for s, p in ASSET_PROFILES.items():
                        if s in YAHOO_SYMBOL_MAP and self._is_stale(self._last_tech.get(s, 0.0), TTL_TECH):
                            tech_tasks[s] = fetch_technicals_with_divergence(session, s, p, ["5m", "1h", "1d"])

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

                    # ── [BREATH] Respirations institutionnelles ───────────────
                    breath_tasks_needed = (
                        self._is_stale(self._last_breath_funding, TTL_BREATH_FAST) or
                        self._is_stale(self._last_breath_oi,      TTL_BREATH_OI)   or
                        self._is_stale(self._last_breath_cvd,     TTL_BREATH_CVD)  or
                        self._is_stale(self._last_breath_cme,     TTL_BREATH_CME)  or
                        self._is_stale(self._last_breath_options, TTL_BREATH_DERIV)
                    )
                    if breath_tasks_needed:
                        _btc_price = _raw_cache.get("binance", {}).get("data", {}).get("btc", 0.0)
                        _br_tasks = []
                        _br_keys  = []
                        if self._is_stale(self._last_breath_funding, TTL_BREATH_FAST):
                            _br_tasks.append(fetch_funding_longshort(session))
                            _br_keys.append("funding")
                        if self._is_stale(self._last_breath_oi, TTL_BREATH_OI):
                            _br_tasks.append(fetch_open_interest(session))
                            _br_keys.append("oi")
                        if self._is_stale(self._last_breath_cvd, TTL_BREATH_CVD):
                            _br_tasks.append(fetch_cvd(session))
                            _br_keys.append("cvd")
                        if self._is_stale(self._last_breath_cme, TTL_BREATH_CME):
                            _br_tasks.append(fetch_cme_gaps(session, _btc_price))
                            _br_keys.append("cme")
                        if self._is_stale(self._last_breath_options, TTL_BREATH_DERIV):
                            _br_tasks.append(fetch_deribit_options(session))
                            _br_keys.append("options")

                        _br_results = await asyncio.gather(*_br_tasks, return_exceptions=True)
                        _br_ok_list = []
                        for _brk, _brr in zip(_br_keys, _br_results):
                            if isinstance(_brr, dict):
                                _raw_cache.setdefault("breath", {})[_brk] = {"data": _brr, "ts": time.time()}
                                setattr(self, f"_last_breath_{_brk}", time.time())
                                if _brr.get("ok"): _br_ok_list.append(_brk)

                        # Recalcul composite si au moins une source OK
                        _br_cache = _raw_cache.get("breath", {})
                        _br_fund  = _br_cache.get("funding",  {}).get("data", {"ok": False, "breath_score": 0.0, "breath_signal": "NEUTRAL", "btc_funding": 0.0, "btc_ls_long_pct": 50.0})
                        _br_oi    = _br_cache.get("oi",       {}).get("data", {"ok": False, "oi_score": 0.0, "oi_signal": "NEUTRAL", "btc_oi_chg_1h": 0.0})
                        _br_cvd   = _br_cache.get("cvd",      {}).get("data", {"ok": False, "cvd_score": 0.0, "cvd_signal": "NEUTRAL", "btc_divergence": False, "btc_cvd_bias": 0.0})
                        _br_cme   = _br_cache.get("cme",      {}).get("data", {"ok": False, "gap_score": 0.0, "gap_signal": "NO_GAP", "nearest_gap": None, "gap_distance": None})
                        _br_opts  = _br_cache.get("options",  {}).get("data", {"ok": False, "options_score": 0.0, "options_signal": "NEUTRAL", "put_call_ratio": 1.0, "iv_skew": 0.0})
                        _br_comp  = compute_breath_composite(_br_fund, _br_oi, _br_cvd, _br_cme, _br_opts)
                        _raw_cache["breath"]["composite"] = {"data": _br_comp, "ts": time.time()}

                        if _br_ok_list:
                            _sig = _br_comp.get("breath_signal", "NEUTRAL")
                            _int = _br_comp.get("breath_intensity", "WEAK")
                            updated.append(f"BREATH({_sig}/{_int} src={','.join(_br_ok_list)})")

                    # ── [GEO-ECO+] Sources géopolitiques et économiques avancées ──
                    _geo_tasks_needed = (
                        self._is_stale(self._last_gdelt,   TTL_GDELT)   or
                        self._is_stale(self._last_fomc,    TTL_FOMC)    or
                        self._is_stale(self._last_pmi,     TTL_PMI)     or
                        self._is_stale(self._last_twitter, TTL_TWITTER) or
                        self._is_stale(self._last_onchain,  TTL_ONCHAIN)  or
                        self._is_stale(self._last_newsapi,  TTL_NEWSAPI)  or
                        self._is_stale(self._last_reddit,   TTL_REDDIT)   or
                        self._is_stale(self._last_econocal, TTL_ECONOCAL)
                    )
                    if _geo_tasks_needed:
                        _geo_tasks = []
                        _geo_keys  = []
                        if self._is_stale(self._last_gdelt,   TTL_GDELT):
                            _geo_tasks.append(fetch_gdelt(session));          _geo_keys.append("gdelt")
                        if self._is_stale(self._last_fomc,    TTL_FOMC):
                            _geo_tasks.append(fetch_fomc_calendar(session));  _geo_keys.append("fomc")
                        if self._is_stale(self._last_pmi,     TTL_PMI):
                            _geo_tasks.append(fetch_pmi_ism(session));        _geo_keys.append("pmi")
                        if self._is_stale(self._last_twitter, TTL_TWITTER):
                            _geo_tasks.append(fetch_twitter_impact(session)); _geo_keys.append("twitter")
                        if self._is_stale(self._last_onchain,  TTL_ONCHAIN):
                            _geo_tasks.append(fetch_onchain_metrics(session));   _geo_keys.append("onchain")
                        if self._is_stale(self._last_newsapi,  TTL_NEWSAPI):
                            _geo_tasks.append(fetch_newsapi_sentiment(session)); _geo_keys.append("newsapi")
                        if self._is_stale(self._last_reddit,   TTL_REDDIT):
                            _geo_tasks.append(fetch_reddit_sentiment(session));  _geo_keys.append("reddit")
                        if self._is_stale(self._last_econocal, TTL_ECONOCAL):
                            _geo_tasks.append(fetch_econocalendar(session));     _geo_keys.append("econocal")

                        _geo_results = await asyncio.gather(*_geo_tasks, return_exceptions=True)
                        _geo_ok = []
                        for _gk, _gr in zip(_geo_keys, _geo_results):
                            if isinstance(_gr, dict):
                                _raw_cache[_gk] = {"data": _gr, "ts": time.time()}
                                setattr(self, f"_last_{_gk}", time.time())
                                if _gr.get("ok"): _geo_ok.append(_gk)

                        # Calcul composite geo_pol_advanced
                        _gdelt_d   = _raw_cache.get("gdelt",   {}).get("data", {"ok": False, "geo_score": 0.0, "risk_signal": "NEUTRAL", "goldstein": 0.0, "tone": 0.0})
                        _fomc_d    = _raw_cache.get("fomc",    {}).get("data", {"ok": False, "fed_score": 0.0, "fed_tone": "NEUTRAL", "fomc_urgency": False, "next_meeting_days": 999})
                        _pmi_d     = _raw_cache.get("pmi",     {}).get("data", {"ok": False, "pmi_score": 0.0, "pmi_signal": "NEUTRAL", "us_pmi_comp": 50.0})
                        _twitter_d = _raw_cache.get("twitter", {}).get("data", {"ok": False, "social_score": 0.0, "social_signal": "NEUTRAL", "vip_alert": False})
                        _geo_adv   = enrich_geo_pol_with_advanced(
                            _raw_cache.get("geo_pol", {}).get("data", {}),
                            _gdelt_d, _fomc_d, _pmi_d, _twitter_d
                        )
                        _raw_cache["geo_pol_advanced"] = {"data": _geo_adv, "ts": time.time()}

                        if _geo_ok:
                            _gsig = _geo_adv.get("advanced_signal", "NEUTRAL")
                            updated.append(f"GEO-ECO+({_gsig} src={','.join(_geo_ok)})")
                            if _fomc_d.get("fomc_urgency"):
                                updated.append("⚠️ FOMC_DEMAIN")

                    # ── [WS-NEW] Corrélations glissantes ─────────────────────
                    if self._is_stale(self._last_correl, TTL_CORREL):
                        _correl = await fetch_rolling_correlations(session)
                        _raw_cache["correlations"] = {"data": _correl, "ts": time.time()}
                        self._last_correl = time.time()
                        if _correl.get("ok"):
                            updated.append(f"CORREL(btc_sp={_correl['correlations'].get('btc_sp500',0):.2f})")

                    # ── [WS-NEW] Liquidations temps réel ─────────────────────
                    if self._is_stale(self._last_liquidations, TTL_LIQUIDATIONS):
                        _liq = await fetch_liquidations(session)
                        _raw_cache["liquidations"] = {"data": _liq, "ts": time.time()}
                        self._last_liquidations = time.time()
                        if _liq.get("ok") and _liq.get("liq_cascade"):
                            updated.append(f"LIQ_CASCADE({_liq['liq_signal']})")

                    # ── [WS-NEW] Order Flow Imbalance ─────────────────────────
                    if self._is_stale(self._last_ofi, TTL_OFI):
                        _ofi = await fetch_order_flow_imbalance(session)
                        _raw_cache["ofi"] = {"data": _ofi, "ts": time.time()}
                        self._last_ofi = time.time()
                        if _ofi.get("ok"):
                            updated.append(f"OFI(btc={_ofi['btc_ofi']:+.2f} {_ofi['ofi_signal']})")

                    # ── [WS-NEW-4b] Liquidity Void Detector (BTC) ─────────────
                    if self._is_stale(self._last_lvd, TTL_LVD):
                        _ofi_for_lvd = _raw_cache.get("ofi", {}).get("data", {"btc_ofi": 0.0})
                        _lvd = await fetch_liquidity_void(session, _ofi_for_lvd)
                        _raw_cache["liquidity_void"] = {"data": _lvd, "ts": time.time()}
                        _raw_cache.setdefault("breath", {})["liquidity_void"] = {"data": _lvd, "ts": time.time()}
                        self._last_lvd = time.time()
                        if _lvd.get("void_detected"):
                            updated.append(f"LVD({_lvd['void_status']} ratio={_lvd['void_ratio']:.2f})")

                    # ── [WS-NEW] Seasonality BTC ──────────────────────────────
                    from datetime import datetime, timezone as _tz
                    _seas = compute_btc_seasonality(hour_utc=datetime.now(_tz.utc).hour)
                    _raw_cache["seasonality"] = {"data": _seas, "ts": time.time()}
                    if _seas["signal"] != "SEASONAL_NEUTRAL":
                        _cme_tag = " CME_EXPIRY" if _seas.get("is_cme_expiry_day") else ""
                        updated.append(f"SEAS({_seas['signal']} {_seas['month_label']} H{_seas['hour_utc']}UTC {_seas['hourly_regime']}{_cme_tag})")

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

        _current_prices_for_eval = {}
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

                # [WS-FIX6] Prix courant pour évaluation différée des sources
                _px = new_state.scores_detail.get("price", 0.0)
                if _px:
                    _current_prices_for_eval[sym] = float(_px)

            except Exception as e:
                logger.error("[COMPUTE] %s: %s", sym, e)

        # [WS-FIX6] Évalue les scores de cycles précédents (~1h) contre le prix actuel
        evaluate_pending_scores(_current_prices_for_eval)

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

                # [V120] MACRO_CONVICTION_GUARD — score macro indépendant du score technique
                _mc_fg          = _raw_cache.get("fg", {}).get("data", {"value": 50})
                _mc_etf         = _raw_cache.get("etf", {}).get(sym, {}).get("data", {"signal": "NEUTRAL"})
                _mc_oi          = _raw_cache.get("breath", {}).get("composite", {}).get("data", {}).get("open_interest", {})
                _mc_fund        = _raw_cache.get("breath", {}).get("composite", {}).get("data", {}).get("funding", {})
                _mc_geo         = _raw_cache.get("geo_pol_advanced", {}).get("data", {})
                macro_conviction_data = compute_macro_conviction(
                    fg                 = _mc_fg,
                    etf_data           = _mc_etf,
                    dxy_momentum       = macro_snap.get("dxy_momentum", 0.0),
                    tnx_momentum       = macro_snap.get("tnx_momentum", 0.0),
                    fomc_days          = _mc_geo.get("fomc_days", 999),
                    gdelt_goldstein    = _mc_geo.get("gdelt_goldstein", 0.0),
                    rsi_1d             = sc.get("rsi_1d", 50.0),
                    risk_off_composite = macro_snap.get("risk_off_composite", 0.5),
                    oi_signal          = _mc_oi.get("oi_signal", "NEUTRAL"),
                    btc_funding        = _mc_fund.get("btc_funding", 0.0),
                )

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
                    # [CONTRARIAN_RSI] RSI journalier + zone contrariante pour MQ5
                    "rsi_1d":            sc.get("rsi_1d", 0.0),
                    "contrarian_zone":   sc.get("contrarian_zone", False),
                    "contrarian_dir":    sc.get("contrarian_dir", ""),
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
                    # [BREATH] Données respirations institutionnelles
                    "breath":            _raw_cache.get("breath", {}).get("composite", {}).get("data", {}),
                    "breath_signal":     _raw_cache.get("breath", {}).get("composite", {}).get("data", {}).get("breath_signal", "NEUTRAL"),
                    "breath_score":      _raw_cache.get("breath", {}).get("composite", {}).get("data", {}).get("breath_composite_score", 0.0),
                    "breath_intensity":  _raw_cache.get("breath", {}).get("composite", {}).get("data", {}).get("breath_intensity", "WEAK"),
                    "breath_reasons":    _raw_cache.get("breath", {}).get("composite", {}).get("data", {}).get("breath_reasons", []),
                    # [GEO-ECO+] Données géopolitiques et économiques avancées
                    "geo_pol_advanced":  _raw_cache.get("geo_pol_advanced", {}).get("data", {}),
                    "fomc_urgency":      _raw_cache.get("geo_pol_advanced", {}).get("data", {}).get("fomc_urgency", False),
                    "fomc_days":         _raw_cache.get("geo_pol_advanced", {}).get("data", {}).get("fomc_days", 999),
                    "fed_tone":          _raw_cache.get("geo_pol_advanced", {}).get("data", {}).get("fed_tone", "NEUTRAL"),
                    "pmi_signal":        _raw_cache.get("geo_pol_advanced", {}).get("data", {}).get("pmi_signal", "NEUTRAL"),
                    "gdelt_goldstein":   _raw_cache.get("geo_pol_advanced", {}).get("data", {}).get("gdelt_goldstein", 0.0),
                    "social_signal":     _raw_cache.get("geo_pol_advanced", {}).get("data", {}).get("social_signal", "NEUTRAL"),
                    "vip_alert":         _raw_cache.get("geo_pol_advanced", {}).get("data", {}).get("vip_alert", False),
                    # [WS-NEW] Données enrichies
                    "correlations":      _raw_cache.get("correlations",{}).get("data",{}),
                    "btc_sp500_correl":  _raw_cache.get("correlations",{}).get("data",{}).get("correlations",{}).get("btc_sp500",0.0),
                    "btc_regime_correl": _raw_cache.get("correlations",{}).get("data",{}).get("btc_regime","INDEPENDENT"),
                    "dxy_pressure":      _raw_cache.get("correlations",{}).get("data",{}).get("dxy_pressure",False),
                    "liquidations":      _raw_cache.get("liquidations",{}).get("data",{}),
                    "liq_signal":        _raw_cache.get("liquidations",{}).get("data",{}).get("liq_signal","NEUTRAL"),
                    "liq_cascade":       _raw_cache.get("liquidations",{}).get("data",{}).get("liq_cascade",False),
                    "ofi":               _raw_cache.get("ofi",{}).get("data",{}),
                    "ofi_signal":        _raw_cache.get("ofi",{}).get("data",{}).get("ofi_signal","BALANCED"),
                    "btc_ofi":           _raw_cache.get("ofi",{}).get("data",{}).get("btc_ofi",0.0),
                    # [WS-NEW-4b] Liquidity Void Detector (BTC)
                    "liquidity_void":         _raw_cache.get("liquidity_void",{}).get("data",{}),
                    "liquidity_void_status":  _raw_cache.get("liquidity_void",{}).get("data",{}).get("void_status","NONE"),
                    "liquidity_void_ratio":   _raw_cache.get("liquidity_void",{}).get("data",{}).get("void_ratio",0.0),
                    "seasonality":       _raw_cache.get("seasonality",{}).get("data",{}),
                    "seasonality_score": _raw_cache.get("seasonality",{}).get("data",{}).get("seasonality_score",0.0),
                    "season_signal":     _raw_cache.get("seasonality",{}).get("data",{}).get("signal","SEASONAL_NEUTRAL"),
                    # [V120] MACRO_CONVICTION_GUARD
                    "macro_conviction":  macro_conviction_data["macro_conviction"],
                    "macro_label":       macro_conviction_data["macro_label"],
                    "fomc_proximity_days": macro_conviction_data["fomc_proximity_days"],
                    "macro_contrarian_damping": macro_conviction_data["contrarian_damping"],
                    "macro_reasons":     macro_conviction_data["reasons"],
                    "source":            "WORLD_SCANNER_v4.0_BOOST",
                    "onchain_signal":    _raw_cache.get("onchain",  {}).get("data", {}).get("onchain_signal",  "NEUTRAL"),
                    "onchain_score":     _raw_cache.get("onchain",  {}).get("data", {}).get("onchain_score",   0.0),
                    "mempool_size":      _raw_cache.get("onchain",  {}).get("data", {}).get("mempool_size",    0),
                    "news_signal":       _raw_cache.get("newsapi",  {}).get("data", {}).get("news_signal",     "NEUTRAL"),
                    "news_score":        _raw_cache.get("newsapi",  {}).get("data", {}).get("news_score",      0.0),
                    "news_bull_pct":     _raw_cache.get("newsapi",  {}).get("data", {}).get("sentiment_pct",   50.0),
                    "reddit_signal":     _raw_cache.get("reddit",   {}).get("data", {}).get("reddit_signal",   "NEUTRAL"),
                    "reddit_bull_pct":   _raw_cache.get("reddit",   {}).get("data", {}).get("reddit_bull_pct", 50.0),
                    "calendar_signal":   _raw_cache.get("econocal", {}).get("data", {}).get("calendar_signal", "CLEAR"),
                    "calendar_score":    _raw_cache.get("econocal", {}).get("data", {}).get("calendar_score",  0.0),
                    "event_upcoming":    _raw_cache.get("econocal", {}).get("data", {}).get("high_impact_upcoming", False),
                    "event_name":        _raw_cache.get("econocal", {}).get("data", {}).get("next_event_name",  ""),
                    "event_minutes":     _raw_cache.get("econocal", {}).get("data", {}).get("next_event_minutes", 999),
                    "timestamp":         datetime.now(timezone.utc).isoformat(),
                    "stale":             False,
                }

        if not results:
            logger.warning("[PUSH] Aucun résultat à pusher")
            return

        payload = {
            "results":   results,
            "scanner":   "WORLD_SCANNER_v3.5_FULL",
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
                    # [V120] Log breath status pour visibilité
                    _br_composite = _raw_cache.get("breath", {}).get("composite", {}).get("data", {})
                    _br_sig  = _br_composite.get("breath_signal", "NO_DATA")
                    _br_int  = _br_composite.get("breath_intensity", "")
                    _br_scr  = _br_composite.get("breath_composite_score", None)
                    _br_ok   = _br_composite.get("ok", False)
                    _breath_log = f"| BREATH={_br_sig}/{_br_int} score={_br_scr:.3f}" if _br_ok and _br_scr is not None else "| BREATH=NO_DATA"
                    logger.info("[PUSH] ✅ %d actifs → serveur | injected=%d %s %s",
                                len(results), d.get("injected", 0),
                                _breath_log,
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
