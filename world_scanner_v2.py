# ================================================================================
# world_scanner_v2.py — OMEGA FUSION BRAIN v2.1
# ================================================================================
#
# SOURCES RÉELLES VÉRIFIÉES (toutes gratuites, sans clé) :
#
#  MACRO :
#   ├── Yahoo Finance      → VIX, DXY, US10Y, SP500, NASDAQ, Gold (v8/finance)
#   ├── Binance            → BTC/ETH prix réel + volume 24h
#   ├── CoinGecko          → BTC dominance, market cap global, chg24h
#   └── FRED St Louis      → Taux Fed officiel (fredgraph.csv)
#
#  SENTIMENT :
#   ├── alternative.me     → Fear & Greed Index + delta 3j
#   └── CoinGecko          → Altcoin season, rotation
#
#  ETF INSTITUTIONNELS :
#   └── Yahoo Finance      → IBIT, GLD, SLV, SPY, QQQ (volume vs moyenne)
#
#  NEWS GÉOPOLITIQUES (RSS sans clé) :
#   ├── BBC World/Business → géopolitique + économie
#   ├── Reuters Business   → flux institutionnels
#   ├── CoinDesk           → news crypto
#   ├── Decrypt            → news crypto (backup)
#   └── ForexFactory       → calendrier économique haute impact
#
#  FALLBACKS EN CASCADE :
#   Si Yahoo 403 → Binance pour prix / CoinGecko pour macro proxy
#   Si CoinGecko 429 → cache précédent (TTL étendu à 10min)
#   Si RSS fails → score news = 0 (neutre, pas de crash)
#   Si TOUT fail → direction = NEUTRAL (jamais de crash)
#
#  ARCHITECTURE :
#   world_scanner (process indépendant)
#     → push POST /push_world_scan → serveur Staline
#     → serveur répond GET /check_omega_veto en < 1ms (lecture RAM)
#
#  INSTALL : pip install aiohttp httpx
#  LANCER  : python world_scanner_v2.py
#  RENDER  : startCommand: python world_scanner_v2.py (Background Worker)
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
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WS] %(message)s",
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
TTL_RSS   = 60
TTL_ETF   = 300
TTL_FG    = 300
TTL_MACRO = 120
TTL_CG    = 180
TTL_FRED  = 3600

VIX_STRESS = 22.0


# ─── MODE SCALP vs SWING ─────────────────────────────────────────────────────
# Basé sur l'analyse des logs réels :
#   - 4217 trades <5min  = scalping dominant
#   - Médiane = 2.2 min
#   - Mais grosses pertes viennent de positions qui restent trop longtemps
#   - Le scanner signale le MODE optimal par actif en temps réel

# Seuils pour déterminer le mode
SCALP_MODE_CONDITIONS = {
    # VIX bas + F&G neutre + pas de news = scalp OK
    "vix_max":        20.0,   # VIX < 20 → marché calme → scalp OK
    "news_score_max":  0.25,  # Pas de news fort → scalp OK
    "fg_range":       (35, 65),  # F&G neutre → scalp OK
    "session_hours": {         # Heures UTC optimales pour scalp
        "EURUSD": list(range(7, 16)),
        "GBPUSD": list(range(7, 16)),
        "USDJPY": list(range(0, 9)) + list(range(12, 17)),
        "XAUUSD": list(range(7, 20)),
        "BTCUSD": list(range(0, 24)),  # 24/7
        "DEFAULT": list(range(7, 20)),
    }
}

SWING_MODE_CONDITIONS = {
    # Conviction macro forte + tendance claire + F&G extrême = swing
    "conviction_min":  0.60,  # Score macro fort → swing trade
    "news_score_min":  0.30,  # News fort → swing (tendance durée)
    "fg_extreme_low":  30,    # F&G < 30 ou > 70 → tendance claire
    "fg_extreme_high": 70,
}

def get_trade_mode(
    sym: str, score: float, conviction: float,
    news_score: float, fg_value: int, vix: float,
    hour_utc: int = -1
) -> Dict:
    """
    Détermine le mode optimal : SCALP, SWING, ou BOTH.
    
    SCALP  → trades courts (<5min), lots normaux, TP serré
    SWING  → trades longs (>30min), lots réduits, TP large
    BOTH   → conditions mixtes, EA choisit selon signal technique
    WAIT   → conditions défavorables pour les deux modes
    
    Retourné dans l'état de l'actif pour que l'EA l'utilise.
    """
    import datetime
    if hour_utc < 0:
        hour_utc = datetime.datetime.utcnow().hour

    mode         = "WAIT"
    scalp_ok     = False
    swing_ok     = False
    scalp_reasons = []
    swing_reasons = []

    abs_score = abs(score)
    dir_str   = "BUY" if score > 0 else ("SELL" if score < 0 else "NEUTRAL")

    # ── SCALP CONDITIONS ──────────────────────────────────────────────────────
    if vix < SCALP_MODE_CONDITIONS["vix_max"]:
        scalp_ok = True
        scalp_reasons.append(f"VIX={vix:.1f}<{SCALP_MODE_CONDITIONS['vix_max']}")
    else:
        scalp_reasons.append(f"VIX={vix:.1f} trop élevé pour scalp")

    # Session optimale pour scalp
    sym_clean  = sym.upper().replace("m","")
    good_hours = SCALP_MODE_CONDITIONS["session_hours"].get(
                     sym_clean,
                     SCALP_MODE_CONDITIONS["session_hours"]["DEFAULT"])
    if hour_utc in good_hours:
        scalp_reasons.append(f"Session H{hour_utc}UTC active")
    else:
        scalp_ok = False
        scalp_reasons.append(f"H{hour_utc}UTC hors session optimale scalp")

    # News fort = danger scalp (spread s'écarte)
    if abs(news_score) > SCALP_MODE_CONDITIONS["news_score_max"]:
        scalp_ok = False
        scalp_reasons.append(f"News fort={news_score:.2f} → risque spread/spike")

    # ── SWING CONDITIONS ──────────────────────────────────────────────────────
    if conviction >= SWING_MODE_CONDITIONS["conviction_min"] and abs_score >= 0.18:
        swing_ok = True
        swing_reasons.append(f"Conviction={conviction:.0%} score={score:+.3f}")

    if abs(news_score) >= SWING_MODE_CONDITIONS["news_score_min"]:
        swing_ok = True
        swing_reasons.append(f"News fort={news_score:+.2f} → tendance")

    if fg_value <= SWING_MODE_CONDITIONS["fg_extreme_low"]:
        swing_ok = True
        swing_reasons.append(f"F&G={fg_value} (Fear) → tendance baissière")
    elif fg_value >= SWING_MODE_CONDITIONS["fg_extreme_high"]:
        swing_ok = True
        swing_reasons.append(f"F&G={fg_value} (Greed) → tendance haussière")

    # ── DÉCISION FINALE ───────────────────────────────────────────────────────
    if swing_ok and scalp_ok:
        mode = "BOTH"
    elif swing_ok and not scalp_ok:
        mode = "SWING"
    elif scalp_ok and not swing_ok:
        mode = "SCALP"
    else:
        mode = "WAIT"  # Conditions défavorables pour les deux

    # Paramètres pratiques pour l'EA selon le mode
    if mode == "SCALP":
        tp_mult  = 1.5    # TP serré
        sl_mult  = 1.0    # SL standard
        lot_mult = 1.0    # Lot normal
        max_hold_min = 10  # Fermer si pas TP après 10 min
    elif mode == "SWING":
        tp_mult  = 3.5    # TP large
        sl_mult  = 1.5    # SL plus large
        lot_mult = 0.70   # Lot réduit (risque plus long)
        max_hold_min = 240  # 4h max
    elif mode == "BOTH":
        tp_mult  = 2.5    # Intermédiaire
        sl_mult  = 1.2
        lot_mult = 0.85
        max_hold_min = 60
    else:  # WAIT
        tp_mult  = 1.0
        sl_mult  = 1.0
        lot_mult = 0.0   # Pas de trade
        max_hold_min = 0

    return {
        "mode":          mode,
        "direction":     dir_str,
        "scalp_ok":      scalp_ok,
        "swing_ok":      swing_ok,
        "scalp_reasons": scalp_reasons,
        "swing_reasons": swing_reasons,
        "tp_mult":       tp_mult,
        "sl_mult":       sl_mult,
        "lot_mult":      lot_mult,
        "max_hold_min":  max_hold_min,
    }


# ─── HEADERS ─────────────────────────────────────────────────────────────────
H_BROWSER  = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
H_BOT      = {"User-Agent": "WorldScanner/2.1 (trading-bot; +https://github.com/noudjistaline1-cloud)"}
H_CURL     = {"User-Agent": "curl/8.4.0", "Accept": "*/*"}


# ================================================================================
# RAM PARTAGÉE — 0ms d'accès
# ================================================================================

@dataclass
class AssetState:
    symbol:          str   = ""
    direction:       str   = "NEUTRAL"
    score:           float = 0.0
    conviction:      float = 0.0
    volatility_regime: str = "NORMAL"
    force_release:   bool  = False
    scores_detail:   Dict  = field(default_factory=dict)
    reasons:         List  = field(default_factory=list)
    last_update:     float = 0.0
    stale:           bool  = True

SHM_STATE: Dict[str, AssetState] = {
    sym: AssetState(symbol=sym) for sym in [
        "BTCUSD","ETHUSD","XAUUSD","XAGUSD",
        "EURUSD","GBPUSD","USDJPY","GBPJPY",
        "XRPUSD","SOLUSD","US30","US100","US500"
    ]
}

ASSET_PROFILES = {
    "BTCUSD": {"type":"crypto",    "geo_pol":"bear", "etf":["IBIT","FBTC"]},
    "ETHUSD": {"type":"crypto",    "geo_pol":"bear", "etf":["ETHA"]},
    "XAUUSD": {"type":"metal",     "geo_pol":"bull", "etf":["GLD","IAU"]},
    "XAGUSD": {"type":"metal",     "geo_pol":"bull", "etf":["SLV"]},
    "EURUSD": {"type":"forex",     "geo_pol":"neutral","etf":["FXE"]},
    "GBPUSD": {"type":"forex",     "geo_pol":"neutral","etf":["FXB"]},
    "USDJPY": {"type":"forex",     "geo_pol":"bear", "etf":["FXY"]},
    "GBPJPY": {"type":"forex",     "geo_pol":"bear", "etf":[]},
    "XRPUSD": {"type":"crypto",    "geo_pol":"bear", "etf":[]},
    "SOLUSD": {"type":"crypto",    "geo_pol":"bear", "etf":[]},
    "US30":   {"type":"index",     "geo_pol":"bear", "etf":["DIA"]},
    "US100":  {"type":"index",     "geo_pol":"bear", "etf":["QQQ"]},
    "US500":  {"type":"index",     "geo_pol":"bear", "etf":["SPY"]},
}

_shm_lock   = threading.Lock()
_raw_cache  = {
    "headlines": {"data":[], "ts":0.0},
    "macro":     {"data":{}, "ts":0.0},
    "fg":        {"data":{}, "ts":0.0},
    "cg":        {"data":{}, "ts":0.0},
    "fred":      {"data":{}, "ts":0.0},
    "binance":   {"data":{}, "ts":0.0},
    "etf":       {},
}


# ================================================================================
# MOTS-CLÉS GÉOPOLITIQUES ET ÉCONOMIQUES
# ================================================================================

# Bearish pour actifs risqués (crypto, indices, forex risk-on)
KW_BEAR_RISK = [
    # Géopolitique
    "war","attack","invasion","missile","strike","conflict","escalat",
    "iran","north korea","russia invad","china taiwan","ceasefire broken",
    "coup","sanctions","embargo","blockade","nuclear",
    # Économique bearish
    "etf outflow","outflows record","sell-off","liquidation","crash",
    "recession","default","debt ceiling","bank run","financial crisis",
    "rate hike","hawkish","tightening","inflation surge","stagflation",
    "tariff","trade war","supply shock",
    # Crypto spécifique
    "bitcoin banned","crypto ban","sec lawsuit","exchange hack","rug pull",
    "whale dump","margin call","funding negative","open interest drop",
]
KW_BULL_RISK = [
    # Paix / détente
    "ceasefire","peace deal","truce","accord","agreement signed",
    # Économie bullish
    "rate cut","dovish","easing","stimulus","qe","quantitative easing",
    "strong gdp","employment up","consumer confidence",
    # Crypto spécifique
    "etf inflow","institutional buying","bitcoin reserve","btc reserve",
    "sec approved","spot etf","strategic reserve","accumulation",
    "rally","breakout","all-time high","ath","adoption","upgrade",
    # Marchés
    "recovery","rebound","surge","bull market","risk-on",
]

# Bullish SPÉCIFIQUE pour or/argent (valeur refuge)
KW_METAL_BULL = [
    "war","conflict","crisis","geopolit","uncertainty","safe haven",
    "inflation","rate cut","dovish","weak dollar","dxy drop",
    "recession fear","gold demand","central bank buying","gold reserve",
    "risk-off","silver shortage",
]
KW_METAL_BEAR = [
    "rate hike","hawkish","dollar surge","strong dollar","usd strength",
    "risk-on","yields rise","real yields","equity rally","dxy rise",
]

RSS_SOURCES = [
    {"url":"https://feeds.bbci.co.uk/news/world/rss.xml",     "name":"BBC_World",    "w":1.4, "cat":"geo"},
    {"url":"https://feeds.bbci.co.uk/news/business/rss.xml",  "name":"BBC_Biz",      "w":1.2, "cat":"eco"},
    {"url":"https://rss.reuters.com/reuters/businessNews",     "name":"Reuters_Biz",  "w":1.4, "cat":"eco"},
    {"url":"https://rss.reuters.com/reuters/topNews",          "name":"Reuters_Top",  "w":1.3, "cat":"geo"},
    {"url":"https://www.coindesk.com/arc/outboundfeeds/rss/",  "name":"CoinDesk",     "w":1.1, "cat":"crypto"},
    {"url":"https://cointelegraph.com/rss",                    "name":"CoinTelegraph","w":1.0, "cat":"crypto"},
    {"url":"https://decrypt.co/feed",                          "name":"Decrypt",      "w":0.9, "cat":"crypto"},
    {"url":"https://feeds.feedburner.com/forexfactory/news",   "name":"ForexFactory", "w":1.2, "cat":"eco"},
    {"url":"https://www.ft.com/rss/home/uk",                   "name":"FT",           "w":1.3, "cat":"eco"},
    {"url":"https://feeds.skynews.com/feeds/rss/world.xml",    "name":"SkyNews",      "w":1.1, "cat":"geo"},
]


# ================================================================================
# FETCHERS — cascade de fallbacks
# ================================================================================

async def fetch_rss(session: aiohttp.ClientSession) -> List[Dict]:
    """Fetch RSS en parallèle avec fallbacks et déduplication."""
    headlines = []
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
                                    "cat":    src.get("cat",""),
                                    "w":      src["w"],
                                })
                        return  # succès
                    elif r.status in (301, 302, 307, 308):
                        continue  # essayer header suivant
            except asyncio.TimeoutError:
                break
            except Exception:
                break

    await asyncio.gather(*[_get(s) for s in RSS_SOURCES], return_exceptions=True)
    logger.info("[RSS] %d titres uniques de %d sources", len(headlines), len(RSS_SOURCES))
    return headlines


async def fetch_macro_yahoo(session: aiohttp.ClientSession) -> Dict:
    """
    Données macro via Yahoo Finance v8 API.
    Cascade de fallbacks si 403 : essaie plusieurs User-Agents + v7 API.
    """
    result = {"vix":18.0,"dxy":101.0,"us10y":4.3,"sp500":5200.0,
              "nasdaq":18000.0,"gold":3300.0,"ok":False,"source":"fallback"}

    tickers_v8 = {
        "vix":    "^VIX",
        "dxy":    "DX-Y.NYB",
        "us10y":  "^TNX",
        "sp500":  "^GSPC",
        "nasdaq": "^IXIC",
        "gold":   "GC=F",
    }

    for k, ticker in tickers_v8.items():
        fetched = False
        # Essayer v8 puis v7
        for api_ver in ["v8", "v7"]:
            for headers in [H_BROWSER, H_BOT]:
                try:
                    if api_ver == "v8":
                        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=3d&interval=1d"
                    else:
                        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?range=3d&interval=1d"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=8),
                                           headers=headers) as r:
                        if r.status == 200:
                            d   = await r.json(content_type=None)
                            cls = d["chart"]["result"][0].get("indicators",{}).get("quote",[{}])[0].get("close",[])
                            vals= [x for x in cls if x is not None]
                            if vals:
                                result[k] = round(float(vals[-1]), 3)
                                result["ok"] = True
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
    """
    Prix et volumes réels via Binance API (source institutionnelle #1).
    Fallback vers CoinGecko si Binance 403.
    """
    result = {"btc":95000.0,"eth":3500.0,"btc_chg24h":0.0,"eth_chg24h":0.0,
              "btc_vol24h_B":0.0,"ok":False,"source":"fallback"}

    symbols = {"BTCUSDT":"btc","ETHUSDT":"eth","SOLUSDT":"sol","XRPUSDT":"xrp"}

    for sym_binance, key in symbols.items():
        for headers in [H_BOT, H_BROWSER, H_CURL]:
            try:
                url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={sym_binance}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=7),
                                       headers=headers) as r:
                    if r.status == 200:
                        d = await r.json(content_type=None)
                        result[key]             = round(float(d["lastPrice"]), 2)
                        result[f"{key}_chg24h"] = round(float(d["priceChangePercent"]), 3)
                        if key == "btc":
                            result["btc_vol24h_B"] = round(float(d["quoteVolume"])/1e9, 2)
                        result["ok"]    = True
                        result["source"]= "binance"
                        break
            except Exception:
                pass

    # Fallback CoinGecko si Binance échoue
    if not result["ok"]:
        for headers in [H_BOT, H_BROWSER]:
            try:
                url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana,ripple&vs_currencies=usd&include_24hr_change=true&include_24hr_vol=true"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8),
                                       headers=headers) as r:
                    if r.status == 200:
                        d = await r.json(content_type=None)
                        result["btc"]        = float(d.get("bitcoin",{}).get("usd",95000))
                        result["btc_chg24h"] = float(d.get("bitcoin",{}).get("usd_24h_change",0))
                        result["eth"]        = float(d.get("ethereum",{}).get("usd",3500))
                        result["eth_chg24h"] = float(d.get("ethereum",{}).get("usd_24h_change",0))
                        result["ok"]         = True
                        result["source"]     = "coingecko_fallback"
                        break
            except Exception:
                pass

    return result


async def fetch_fear_greed(session: aiohttp.ClientSession) -> Dict:
    """Fear & Greed Index avec delta 3 jours — alternative.me."""
    result = {"value":50,"label":"Neutral","delta_1d":0,"delta_3d":0,
              "trend":"STABLE","ok":False}
    for headers in [H_BOT, H_BROWSER, H_CURL]:
        try:
            async with session.get("https://api.alternative.me/fng/?limit=4&format=json",
                                   timeout=aiohttp.ClientTimeout(total=7),
                                   headers=headers) as r:
                if r.status == 200:
                    text = await r.text()
                    data = json.loads(text).get("data",[])
                    if data:
                        val   = int(data[0]["value"])
                        prev1 = int(data[1]["value"]) if len(data)>1 else val
                        prev3 = int(data[3]["value"]) if len(data)>3 else val
                        d1    = val - prev1
                        d3    = val - prev3
                        result = {
                            "value":    val,
                            "label":    data[0]["value_classification"],
                            "delta_1d": d1,
                            "delta_3d": d3,
                            "trend":    ("IMPROVING" if d3>5 else
                                         "WORSENING" if d3<-5 else "STABLE"),
                            "ok": True,
                        }
                        return result
        except Exception:
            pass
    return result


async def fetch_coingecko_global(session: aiohttp.ClientSession) -> Dict:
    """BTC dominance + market cap global via CoinGecko."""
    result = {"btc_dominance":50.0,"eth_dominance":15.0,"market_chg_24h":0.0,
              "altcoin_season":False,"total_mcap_T":0.0,"ok":False}
    for headers in [H_BOT, H_BROWSER]:
        try:
            async with session.get("https://api.coingecko.com/api/v3/global",
                                   timeout=aiohttp.ClientTimeout(total=8),
                                   headers=headers) as r:
                if r.status == 200:
                    d   = await r.json(content_type=None)
                    dat = d.get("data",{})
                    dom = dat.get("market_cap_percentage",{})
                    btc_d = float(dom.get("btc",50))
                    mcap  = dat.get("total_market_cap",{}).get("usd",0)
                    result = {
                        "btc_dominance":  round(btc_d, 1),
                        "eth_dominance":  round(float(dom.get("eth",15)), 1),
                        "market_chg_24h": round(float(dat.get("market_cap_change_percentage_24h_usd",0)), 2),
                        "altcoin_season": btc_d < 45.0,
                        "total_mcap_T":   round(float(mcap)/1e12, 2) if mcap else 0.0,
                        "ok": True,
                    }
                    return result
        except Exception:
            pass
    return result


async def fetch_etf_flows(session: aiohttp.ClientSession, sym: str, etfs: List[str]) -> Dict:
    """Flux ETF via volume Yahoo Finance — indicateur institutionnel clé."""
    if not etfs:
        return {"signal":"NEUTRAL","ok":False,"details":[]}

    all_results = []
    for etf in etfs[:2]:
        for headers in [H_BROWSER, H_BOT]:
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{etf}?range=15d&interval=1d"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=9),
                                       headers=headers) as r:
                    if r.status == 200:
                        d    = await r.json(content_type=None)
                        res0 = d["chart"]["result"][0]
                        q    = res0.get("indicators",{}).get("quote",[{}])[0]
                        vols = [v for v in q.get("volume",[]) if v and v>0]
                        cls  = [c for c in q.get("close",[])  if c and c>0]

                        if len(vols) >= 5:
                            # Moyenne des 10 jours précédents
                            avg   = sum(vols[-11:-1]) / min(10, max(len(vols)-1, 1))
                            last  = vols[-1]
                            ratio = last / avg if avg > 0 else 1.0
                            price = cls[-1] if cls else 1.0

                            # Calculer aussi la tendance 3j
                            trend3 = sum(vols[-4:-1]) / (3 * avg) if avg > 0 else 1.0

                            if ratio > 1.30:   sig = "STRONG_INFLOW"
                            elif ratio > 1.10: sig = "INFLOW"
                            elif ratio < 0.70: sig = "STRONG_OUTFLOW"
                            elif ratio < 0.88: sig = "OUTFLOW"
                            else:              sig = "NEUTRAL"

                            all_results.append({
                                "etf":      etf,
                                "signal":   sig,
                                "ratio":    round(ratio, 2),
                                "trend_3d": round(trend3, 2),
                                "flow_M$":  round(last * price / 1e6, 1),
                                "last_vol": int(last),
                                "avg_vol":  int(avg),
                            })
                        break  # succès pour cet ETF
            except Exception:
                pass

    if not all_results:
        return {"signal":"NEUTRAL","ok":False,"details":[]}

    bull = sum(1 for r in all_results if r["signal"] in ("INFLOW","STRONG_INFLOW"))
    bear = sum(1 for r in all_results if r["signal"] in ("OUTFLOW","STRONG_OUTFLOW"))
    strong = any(r["signal"] in ("STRONG_INFLOW","STRONG_OUTFLOW") for r in all_results)

    if bull > bear:   consolidated = "STRONG_INFLOW"  if strong else "INFLOW"
    elif bear > bull: consolidated = "STRONG_OUTFLOW" if strong else "OUTFLOW"
    else:             consolidated = "NEUTRAL"

    return {
        "signal":        consolidated,
        "details":       all_results,
        "total_flow_M$": round(sum(r.get("flow_M$",0) for r in all_results), 1),
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
                    lines = [l for l in text.strip().split("\n") if "," in l and not l.startswith("DATE")]
                    if lines:
                        last = lines[-1].split(",")
                        if len(last) >= 2 and last[1].strip() != ".":
                            result["fed_rate"]  = round(float(last[1].strip()), 2)
                            result["fed_date"]  = last[0].strip()
                            result["ok"]        = True
                            result["source"]    = "FRED"
                            return result
        except Exception:
            pass
    return result


# ================================================================================
# SCORING AVEC POIDS ADAPTATIFS (VIX-driven)
# ================================================================================

def compute_weights(vix: float, urgency: bool = False) -> Dict[str, float]:
    """
    Poids adaptatifs selon le régime VIX.
    Urgence géopolitique → ETF et news dominent encore plus.
    """
    if urgency or vix > 30:
        # Crise majeure : seuls les flux réels et les news comptent
        return {"news":0.42, "etf":0.40, "fg":0.05, "macro":0.13}
    elif vix > VIX_STRESS:
        # Stress modéré : flux ETF et news géopolit dominent
        return {"news":0.38, "etf":0.36, "fg":0.08, "macro":0.18}
    else:
        # Marché calme : sentiment reprend de l'importance
        return {"news":0.30, "etf":0.28, "fg":0.22, "macro":0.20}


def score_news(headlines: List[Dict], profile: Dict) -> Tuple[float, List[str]]:
    """Score des news géopolitiques/économiques : -1.0 à +1.0."""
    geo_pol = profile.get("geo_pol","bear")
    typ     = profile.get("type","forex")
    bull_kw = KW_METAL_BULL if typ=="metal" else KW_BULL_RISK
    bear_kw = KW_METAL_BEAR if typ=="metal" else KW_BEAR_RISK

    bull_s = bear_s = 0.0
    triggered = []

    for h in headlines[:60]:
        w   = h["w"]
        hb  = sum(1 for kw in bull_kw if kw in h["text"])
        hbr = sum(1 for kw in bear_kw if kw in h["text"])

        if hb or hbr:
            triggered.append(f"[{h['source']}] {h['title'][:80]}")

        if geo_pol == "bull":
            # Or/argent : crise géo = BULLISH
            bull_s += hbr * w * 0.9 + hb * w * 0.5
        else:
            bear_s += hbr * w * 0.9
            bull_s += hb  * w * 0.5

    total = (bull_s + bear_s) or 1.0
    score = round(max(-1.0, min(1.0, (bull_s - bear_s) / total)), 3)
    return score, triggered[:6]


def score_etf(etf_data: Dict) -> float:
    """Score ETF flows : -1.0 à +1.0."""
    m = {
        "STRONG_INFLOW":  +0.90,
        "INFLOW":         +0.50,
        "NEUTRAL":         0.00,
        "OUTFLOW":        -0.50,
        "STRONG_OUTFLOW": -0.90,
        "UNKNOWN":         0.00,
    }
    base = m.get(etf_data.get("signal","NEUTRAL"), 0.0)

    # Amplifier si tendance 3j confirme
    details = etf_data.get("details",[])
    if details:
        trend3 = details[0].get("trend_3d", 1.0)
        if trend3 < 0.80 and base < 0:
            base = max(-1.0, base * 1.15)  # outflow persistant → amplifier
        elif trend3 > 1.20 and base > 0:
            base = min(1.0,  base * 1.15)  # inflow persistant → amplifier

    return round(base, 3)


def score_fear_greed(fg: Dict, profile: Dict) -> float:
    """Score F&G : inverse pour métaux, direct pour le reste."""
    val  = fg.get("value", 50)
    d3   = fg.get("delta_3d", 0)
    typ  = profile.get("type", "forex")

    # Score de base : -1.0 (peur extrême) à +1.0 (euphorie)
    base = -(val - 50) / 50.0 if typ == "metal" else (val - 50) / 50.0

    # Tendance 3j amplifie le signal
    if abs(d3) > 8:
        up = d3 > 0
        if (up and typ != "metal") or (not up and typ == "metal"):
            base = max(-1.0, min(1.0, base + 0.22))
        else:
            base = max(-1.0, min(1.0, base - 0.22))

    return round(base, 3)


def score_macro(macro: Dict, binance: Dict, cg: Dict, fred: Dict, profile: Dict) -> float:
    """Score macro en utilisant TOUTES les sources disponibles."""
    typ    = profile.get("type","forex")
    vix    = float(macro.get("vix",18) or 18)
    dxy    = float(macro.get("dxy",101) or 101)
    u10    = float(macro.get("us10y",4.3) or 4.3)
    spx    = float(macro.get("sp500",5200) or 5200)
    nasdaq = float(macro.get("nasdaq",18000) or 18000)

    # Prix BTC depuis Binance (source plus fiable que proxy)
    btc       = float(binance.get("btc",95000) or 95000)
    btc_chg   = float(binance.get("btc_chg24h",0) or 0)
    btc_dom   = float(cg.get("btc_dominance",50) or 50)
    mkt_chg   = float(cg.get("market_chg_24h",0) or 0)
    fed_rate  = float(fred.get("fed_rate",4.5) or 4.5)

    if typ == "metal":
        s = 0.0
        # DXY : or inverse du dollar
        if dxy > 105: s -= 0.50
        elif dxy > 103: s -= 0.25
        elif dxy < 98: s += 0.50
        elif dxy < 100: s += 0.25
        # VIX : or = valeur refuge en stress
        if vix > 30: s += 0.40
        elif vix > 25: s += 0.22
        elif vix > 22: s += 0.10
        elif vix < 12: s -= 0.15
        # Taux : hausse = bearish or
        if u10 > 5.0: s -= 0.35
        elif u10 > 4.7: s -= 0.18
        elif u10 < 3.5: s += 0.25
        elif u10 < 4.0: s += 0.10
        # Fed
        if fed_rate > 5.0: s -= 0.15
        elif fed_rate < 3.5: s += 0.15

    elif typ == "crypto":
        s = 0.0
        # VIX : risque systémique
        if vix > 32: s -= 0.60
        elif vix > 28: s -= 0.35
        elif vix > 23: s -= 0.15
        elif vix < 14: s += 0.20
        # SP500 : corrélation risk-on
        if spx > 5500: s += 0.25
        elif spx > 5200: s += 0.12
        elif spx < 4800: s -= 0.25
        elif spx < 4500: s -= 0.40
        # BTC momentum (pour ETH/SOL/XRP)
        if "BTCUSD" not in profile.get("sym",""):
            if btc_chg > 3: s += 0.15
            elif btc_chg < -3: s -= 0.20
        # Taux Fed : coût capital crypto
        if u10 > 5.0: s -= 0.30
        elif u10 > 4.7: s -= 0.15
        elif u10 < 3.5: s += 0.20
        # DXY (USD fort = crypto baisse en USD)
        if dxy > 105: s -= 0.25
        elif dxy < 98: s += 0.20
        # Market cap chg (momentum global crypto)
        if mkt_chg > 5: s += 0.10
        elif mkt_chg < -5: s -= 0.15

    elif typ == "index":
        s = 0.0
        if vix > 30: s -= 0.45
        elif vix > 24: s -= 0.20
        elif vix < 14: s += 0.22
        if spx > 5400: s += 0.20
        elif spx < 4800: s -= 0.30
        if u10 > 5.0: s -= 0.25
        if fed_rate > 5.5: s -= 0.15

    else:  # forex
        s = 0.0
        if vix > 27: s -= 0.20
        if "USD" in profile.get("sym",""):
            if dxy > 103: s += 0.15
            elif dxy < 99: s -= 0.15

    return round(max(-1.0, min(1.0, s)), 3)


# ================================================================================
# MOTEUR DE DÉCISION
# ================================================================================

def compute_asset_state(
    sym: str, profile: Dict,
    headlines: List[Dict],
    etf_data: Dict,
    fg: Dict, macro: Dict, binance: Dict, cg: Dict, fred: Dict,
    urgency: bool = False,
) -> AssetState:
    """Calcule l'état complet d'un actif avec tous les signaux."""
    vix    = float(macro.get("vix",18) or 18)
    regime = "CRITICAL" if vix>30 else ("HIGH" if vix>VIX_STRESS else "NORMAL")

    # Injecter le symbole dans le profil pour score_macro
    profile_with_sym = {**profile, "sym": sym}

    w = compute_weights(vix, urgency)

    news_sc, triggers = score_news(headlines, profile)
    etf_sc            = score_etf(etf_data)
    fg_sc             = score_fear_greed(fg, profile)
    mac_sc            = score_macro(macro, binance, cg, fred, profile_with_sym)

    # Score final pondéré
    final = round(max(-1.0, min(1.0,
        news_sc * w["news"] +
        etf_sc  * w["etf"]  +
        fg_sc   * w["fg"]   +
        mac_sc  * w["macro"]
    )), 4)

    # ── Direction ────────────────────────────────────────────────────────────
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
        direction = "RESPIRATION"  # pause — signaux contradictoires
    else:
        direction = "NEUTRAL"

    conviction = round(min(1.0, abs(final) * 1.8), 2)

    # ── Raisons lisibles ─────────────────────────────────────────────────────
    reasons = []
    if force_release:
        reasons.append(f"⚠️ FORCE_RELEASE — score CRITIQUE={final:.3f}")
    if triggers:
        reasons.append(f"[NEWS/{triggers[0].split(']')[0][1:]}] {triggers[0].split('] ')[1][:70]}")
    if abs(news_sc) > 0.15:
        reasons.append(f"[NEWS] {'▲' if news_sc>0 else '▼'}{news_sc:+.2f} ({len(triggers)} titres)")

    etf_sig = etf_data.get("signal","?")
    if etf_data.get("ok") and etf_sig != "NEUTRAL":
        fl  = etf_data.get("total_flow_M$",0)
        det = etf_data.get("details",[])
        etf_name = det[0].get("etf","?") if det else "?"
        reasons.append(f"[ETF/{etf_name}] {etf_sig} {fl:+.0f}M$")

    fg_v = fg.get("value",50)
    reasons.append(f"[SENTIMENT] F&G={fg_v}/100 ({fg.get('label','?')}) "
                   f"Δ1j={fg.get('delta_1d',0):+d} Δ3j={fg.get('delta_3d',0):+d} → {fg.get('trend','?')}")

    vix_v = macro.get("vix",18)
    dxy_v = macro.get("dxy",101)
    u10_v = macro.get("us10y",4.3)
    btc_v = binance.get("btc",95000)
    btc_c = binance.get("btc_chg24h",0)
    reasons.append(f"[MACRO] VIX={vix_v:.1f} DXY={dxy_v:.2f} US10Y={u10_v:.2f}%")

    if binance.get("ok"):
        reasons.append(f"[PRIX] BTC=${btc_v:,.0f} ({btc_c:+.2f}%) "
                       f"vol={binance.get('btc_vol24h_B',0):.1f}B$")
    if cg.get("ok"):
        reasons.append(f"[CRYPTO_MKT] BTC_DOM={cg.get('btc_dominance',50):.1f}% "
                       f"MCap24h={cg.get('market_chg_24h',0):+.2f}%")
    if fred.get("ok"):
        reasons.append(f"[FED] Taux={fred.get('fed_rate',4.5):.2f}% ({fred.get('fed_date','')})")

    # ── Mode SCALP vs SWING ──────────────────────────────────────────────────
    import datetime as _dt
    hour_utc   = _dt.datetime.utcnow().hour
    trade_mode = get_trade_mode(
        sym, final, conviction,
        news_sc, fg.get("value", 50),
        vix, hour_utc
    )

    return AssetState(
        symbol           = sym,
        direction        = direction,
        score            = final,
        conviction       = conviction,
        volatility_regime= regime,
        force_release    = force_release,
        scores_detail    = {
            "news":  news_sc, "etf": etf_sc,
            "fg":    fg_sc,   "macro": mac_sc,
            "weights": w, "vix": vix,
            "trade_mode": trade_mode,
        },
        reasons     = reasons,
        last_update = time.time(),
        stale       = False,
    )


# ================================================================================
# WATCHDOG — Anti-corruption du cache
# ================================================================================

def validate_state(state: AssetState) -> bool:
    """Valide qu'un état est cohérent avant de l'injecter en RAM."""
    if not state.symbol:
        return False
    if state.direction not in ("BUY","SELL","NEUTRAL","RESPIRATION","CRITICAL"):
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
        self._running     = True
        self._last_rss    = 0.0
        self._last_macro  = 0.0
        self._last_fg     = 0.0
        self._last_cg     = 0.0
        self._last_fred   = 0.0
        self._last_binance= 0.0
        self._last_etf    : Dict[str, float] = {}
        self._last_push   = 0.0
        self._urgency     = False
        self._cycle_count = 0
        # Compteurs de fiabilité des sources
        self._source_health = {
            "yahoo":    {"ok":0,"fail":0},
            "binance":  {"ok":0,"fail":0},
            "coingecko":{"ok":0,"fail":0},
            "rss":      {"ok":0,"fail":0},
            "fg":       {"ok":0,"fail":0},
            "fred":     {"ok":0,"fail":0},
        }

    def _is_stale(self, ts: float, ttl: float) -> bool:
        effective_ttl = ttl * 0.5 if self._urgency else ttl
        return (time.time() - ts) >= effective_ttl

    def _track(self, source: str, ok: bool):
        if source in self._source_health:
            if ok: self._source_health[source]["ok"]   += 1
            else:  self._source_health[source]["fail"] += 1

    def _health_pct(self, source: str) -> float:
        h = self._source_health.get(source, {"ok":0,"fail":0})
        total = h["ok"] + h["fail"]
        return (h["ok"] / total * 100) if total > 0 else 100.0

    async def run(self):
        logger.info("="*65)
        logger.info("  WORLD SENTINEL v2.1 OMEGA FUSION")
        logger.info("  Serveur : %s", SERVER_URL)
        logger.info("  Actifs  : %d | Push : %ds", len(SHM_STATE), PUSH_INTERVAL)
        logger.info("="*65)

        timeout = aiohttp.ClientTimeout(total=15)

        while self._running:
            t0 = time.time()
            self._cycle_count += 1
            updated = []

            try:
                connector = aiohttp.TCPConnector(limit=20, limit_per_host=4)
                async with aiohttp.ClientSession(timeout=timeout,
                                                  connector=connector) as session:

                    # ── RSS ───────────────────────────────────────────────────
                    if self._is_stale(self._last_rss, TTL_RSS):
                        headlines = await fetch_rss(session)
                        ok = len(headlines) > 3
                        self._track("rss", ok)
                        if ok:
                            _raw_cache["headlines"] = {"data": headlines, "ts": time.time()}
                            self._last_rss = time.time()
                            updated.append(f"RSS({len(headlines)})")

                            # Détection urgence géopolitique
                            urgent_kw = ["war ","invasion","missile","nuclear","ceasefire broken",
                                         "oil shock","bank run","financial crisis"]
                            cnt = sum(1 for h in headlines[:25] for kw in urgent_kw
                                      if kw in h["text"])
                            prev_urgency = self._urgency
                            self._urgency = cnt >= 3
                            if self._urgency and not prev_urgency:
                                logger.warning("[SENTINEL] 🚨 URGENCE GÉOPOLITIQUE — refresh x2")
                        else:
                            logger.warning("[SENTINEL] RSS insuffisant (%d titres)", len(headlines))

                    # ── Macro Yahoo ───────────────────────────────────────────
                    if self._is_stale(self._last_macro, TTL_MACRO):
                        macro = await fetch_macro_yahoo(session)
                        ok = macro.get("ok", False)
                        self._track("yahoo", ok)
                        _raw_cache["macro"] = {"data": macro, "ts": time.time()}
                        self._last_macro = time.time()
                        if ok:
                            updated.append(f"MACRO(VIX={macro.get('vix',0):.1f})")
                        else:
                            logger.warning("[SENTINEL] Yahoo macro FAIL — utilise cache")

                    # ── Binance prix réels ────────────────────────────────────
                    if self._is_stale(self._last_binance, 60):  # Prix crypto : toutes les 60s
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
                            updated.append(f"CG(BTC_DOM={cg.get('btc_dominance',50):.0f}%)")

                    # ── FRED taux Fed ─────────────────────────────────────────
                    if self._is_stale(self._last_fred, TTL_FRED):
                        fred = await fetch_fred_rate(session)
                        ok = fred.get("ok", False)
                        self._track("fred", ok)
                        _raw_cache["fred"] = {"data": fred, "ts": time.time()}
                        self._last_fred = time.time()
                        if ok:
                            updated.append(f"FED={fred.get('fed_rate',0):.2f}%")

                    # ── ETF par actif (parallèle) ─────────────────────────────
                    etf_tasks = {}
                    for sym, profile in ASSET_PROFILES.items():
                        if self._is_stale(self._last_etf.get(sym, 0.0), TTL_ETF):
                            etfs = profile.get("etf",[])
                            if etfs:
                                etf_tasks[sym] = fetch_etf_flows(session, sym, etfs)

                    if etf_tasks:
                        etf_ress = await asyncio.gather(
                            *etf_tasks.values(), return_exceptions=True
                        )
                        etf_ok = 0
                        for sym, r in zip(etf_tasks.keys(), etf_ress):
                            if isinstance(r, dict):
                                _raw_cache["etf"][sym] = {"data": r, "ts": time.time()}
                                self._last_etf[sym]    = time.time()
                                if r.get("ok"): etf_ok += 1
                        if etf_ok:
                            updated.append(f"ETF×{etf_ok}")

                # ── Recalcul états ────────────────────────────────────────────
                if updated:
                    await self._recompute_all()
                    logger.info("[CYCLE %d] Updated: %s", self._cycle_count, " | ".join(updated))

                # ── Push vers serveur ─────────────────────────────────────────
                if (time.time() - self._last_push) >= PUSH_INTERVAL:
                    await self._push_to_server()
                    self._last_push = time.time()

                # Log santé sources toutes les 20 cycles
                if self._cycle_count % 20 == 0:
                    self._log_source_health()

            except Exception as e:
                logger.error("[SENTINEL] Erreur cycle %d: %s", self._cycle_count, e)

            # Pause adaptative : 5s normal, 2s en urgence
            pause = 2.0 if self._urgency else 5.0
            dt    = time.time() - t0
            await asyncio.sleep(max(0.5, pause - dt))

    async def _recompute_all(self):
        """Recalcule l'état de tous les actifs."""
        headlines = _raw_cache["headlines"].get("data", [])
        macro     = _raw_cache["macro"].get("data", {})
        fg        = _raw_cache["fg"].get("data", {"value":50,"ok":False})
        cg        = _raw_cache["cg"].get("data", {"ok":False})
        fred      = _raw_cache["fred"].get("data", {"fed_rate":4.5,"ok":False})
        binance   = _raw_cache["binance"].get("data", {"btc":95000,"ok":False})

        if not macro and not binance.get("ok"):
            logger.warning("[COMPUTE] Données macro manquantes — skip")
            return

        for sym, profile in ASSET_PROFILES.items():
            try:
                etf_d = _raw_cache["etf"].get(sym, {}).get("data",
                        {"signal":"NEUTRAL","ok":False})

                new_state = compute_asset_state(
                    sym, profile,
                    headlines, etf_d,
                    fg, macro, binance, cg, fred,
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
        """Log un résumé propre dans le terminal."""
        with _shm_lock:
            states = dict(SHM_STATE)

        buys = [s for s,v in states.items() if v.direction=="BUY"]
        sells= [s for s,v in states.items() if v.direction=="SELL"]
        crits= [s for s,v in states.items() if v.direction=="CRITICAL"]
        resp = [s for s,v in states.items() if v.direction=="RESPIRATION"]

        vix   = _raw_cache["macro"].get("data",{}).get("vix",18) or 18
        btc_p = _raw_cache["binance"].get("data",{}).get("btc",0)
        fg_v  = _raw_cache["fg"].get("data",{}).get("value",50)

        logger.info("─"*65)
        if crits: logger.warning("  ⚠️  CRITICAL : %s", ", ".join(crits))
        if buys:  logger.info("  🟢 BUY       : %s", ", ".join(buys))
        if sells: logger.info("  🔴 SELL      : %s", ", ".join(sells))
        if resp:  logger.info("  🔵 RESPIRATION: %s", ", ".join(resp))
        logger.info("  VIX=%.1f | BTC=$%s | F&G=%d | Urgence=%s",
                    vix, f"{btc_p:,.0f}" if btc_p else "?", fg_v, self._urgency)
        logger.info("─"*65)
        for sym, st in states.items():
            if st.stale:
                continue
            icon = {"BUY":"🟢","SELL":"🔴","CRITICAL":"⚠️ ","RESPIRATION":"🔵","NEUTRAL":"⬜"}.get(st.direction,"⬜")
            r0   = st.reasons[1][:55] if len(st.reasons)>1 else (st.reasons[0][:55] if st.reasons else "")
            logger.info("  %s %-10s %-13s sc=%+.3f cv=%.0f%%  %s",
                        icon, sym, st.direction, st.score, st.conviction*100, r0)

    def _log_source_health(self):
        """Log la fiabilité des sources."""
        logger.info("[HEALTH] Sources — ", )
        for src, h in self._source_health.items():
            pct = self._health_pct(src)
            icon= "✅" if pct>=80 else ("⚠️ " if pct>=50 else "❌")
            logger.info("  %s %s: %.0f%% (%d/%d)",
                        icon, src.upper(), pct, h["ok"], h["ok"]+h["fail"])

    async def _push_to_server(self):
        """Push tous les états vers /push_world_scan."""
        with _shm_lock:
            results = {}
            for sym, st in SHM_STATE.items():
                if st.stale:
                    continue
                results[sym] = {
                    "symbol":            st.symbol,
                    "direction":         1 if st.direction=="BUY" else
                                        (-1 if st.direction in ("SELL","CRITICAL") else 0),
                    "direction_label":   st.direction,
                    "strength":          ("STRONG" if st.conviction>=0.70 else
                                         "MODERATE" if st.conviction>=0.45 else "WEAK"),
                    "decision_score":    st.score,
                    "conviction":        st.conviction,
                    "volatility_regime": st.volatility_regime,
                    "force_release":     st.force_release,
                    "reasons":           st.reasons,
                    "scores":            st.scores_detail,
                    "trade_mode":        st.scores_detail.get("trade_mode",{}),
                    "source":            "WORLD_SCANNER_v2.1",
                    "timestamp":         datetime.now(timezone.utc).isoformat(),
                    "stale":             False,
                }

        if not results:
            logger.warning("[PUSH] Aucun résultat à pusher")
            return

        payload = {
            "results":   results,
            "scanner":   "WORLD_SCANNER_v2.1",
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
                    crit_syms = [s for s,v in results.items() if v.get("force_release")]
                    logger.info("[PUSH] ✅ %d actifs → serveur | injected=%d %s",
                                len(results), d.get("injected",0),
                                f"| ⚠️ CRITICAL={crit_syms}" if crit_syms else "")
                else:
                    logger.warning("[PUSH] HTTP %d: %s", r.status_code, r.text[:80])
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
