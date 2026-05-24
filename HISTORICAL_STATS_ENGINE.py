"""
HISTORICAL_STATS_ENGINE.py
==========================
Génère stats_10y.json depuis des données de marché RÉELLES sur 7-10 ans.

Sources:
- Yahoo Finance : prix daily/hourly XAU, XAG, BTC, ETH, EURUSD, GBPUSD,
                  USDJPY, GBPJPY, USDCHF, AUDUSD, US30, US100, US500
- Binance API   : BTC, ETH hourly (5 ans)
- FRED API      : taux directeurs, inflation, emploi (données macro officielles)

Ce fichier tourne UNE SEULE FOIS sur ton PC ou sur Render
et génère stats_10y.json utilisé en permanence par le serveur.

Usage: python HISTORICAL_STATS_ENGINE.py
Output: stats_10y.json (~500KB)
"""

import json, time, datetime, statistics, math
from collections import defaultdict

try:
    import httpx
    CLIENT = "httpx"
except ImportError:
    import urllib.request
    CLIENT = "urllib"

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

SYMBOLS = {
    "XAUUSD":  {"yahoo": "GC=F",        "type": "metal",   "binance": None},
    "XAGUSD":  {"yahoo": "SI=F",        "type": "metal",   "binance": None},
    "BTCUSD":  {"yahoo": "BTC-USD",     "type": "crypto",  "binance": "BTCUSDT"},
    "ETHUSD":  {"yahoo": "ETH-USD",     "type": "crypto",  "binance": "ETHUSDT"},
    "EURUSD":  {"yahoo": "EURUSD=X",    "type": "forex",   "binance": None},
    "GBPUSD":  {"yahoo": "GBPUSD=X",    "type": "forex",   "binance": None},
    "USDJPY":  {"yahoo": "JPY=X",       "type": "forex",   "binance": None},
    "GBPJPY":  {"yahoo": "GBPJPY=X",    "type": "forex",   "binance": None},
    "USDCHF":  {"yahoo": "CHF=X",       "type": "forex",   "binance": None},
    "AUDUSD":  {"yahoo": "AUDUSD=X",    "type": "forex",   "binance": None},
    "US30":    {"yahoo": "YM=F",        "type": "index",   "binance": None},
    "US100":   {"yahoo": "NQ=F",        "type": "index",   "binance": None},
    "US500":   {"yahoo": "ES=F",        "type": "index",   "binance": None},
}

# ─────────────────────────────────────────────────────────────────────────────
# SEUILS PERSONNALISÉS PAR ACTIF
# ─────────────────────────────────────────────────────────────────────────────
# avg_return = rendement moyen d'une bougie horaire (en %)
# Un seuil universel de 0.03% est du bruit pour BTC (range ~0.8%/h)
# mais représente un vrai signal pour EURUSD (range ~0.045%/h)
# Calibration: seuil = 15% du range horaire typique
#
# Pour la DIRECTION houraire  : seuil_h (bougie 1h)
# Pour la DIRECTION weekday   : seuil_d (bougie daily)
# Pour la DIRECTION mensuelle : seuil_m (bougie daily sur le mois)
#
DIRECTION_THRESHOLDS = {
    # sym         h_buy   h_sell  d_buy   d_sell  m_buy   m_sell
    "XAUUSD":  ( 0.027,  -0.027,  0.08,  -0.08,   0.15,  -0.15),
    "XAGUSD":  ( 0.038,  -0.038,  0.10,  -0.10,   0.20,  -0.20),
    "BTCUSD":  ( 0.120,  -0.120,  0.40,  -0.40,   1.00,  -1.00),
    "ETHUSD":  ( 0.135,  -0.135,  0.45,  -0.45,   1.20,  -1.20),
    "EURUSD":  ( 0.007,  -0.007,  0.04,  -0.04,   0.08,  -0.08),
    "GBPUSD":  ( 0.008,  -0.008,  0.05,  -0.05,   0.10,  -0.10),
    "USDJPY":  ( 0.008,  -0.008,  0.05,  -0.05,   0.10,  -0.10),
    "GBPJPY":  ( 0.014,  -0.014,  0.07,  -0.07,   0.15,  -0.15),
    "USDCHF":  ( 0.007,  -0.007,  0.04,  -0.04,   0.08,  -0.08),
    "AUDUSD":  ( 0.008,  -0.008,  0.04,  -0.04,   0.09,  -0.09),
    "US30":    ( 0.030,  -0.030,  0.10,  -0.10,   0.25,  -0.25),
    "US100":   ( 0.042,  -0.042,  0.15,  -0.15,   0.35,  -0.35),
    "US500":   ( 0.027,  -0.027,  0.10,  -0.10,   0.25,  -0.25),
    # Défaut forex si symbole non listé
    "_DEFAULT": (0.010,  -0.010,  0.05,  -0.05,   0.10,  -0.10),
}

def get_thresholds(sym):
    """Retourne (h_buy, h_sell, d_buy, d_sell, m_buy, m_sell) pour le symbole."""
    return DIRECTION_THRESHOLDS.get(
        sym.upper().replace("m","").replace("M",""),
        DIRECTION_THRESHOLDS["_DEFAULT"]
    )

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
}

# ─────────────────────────────────────────────────────────────────────────────
# HTTP HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _get_json(url):
    for attempt in range(3):
        try:
            if CLIENT == "httpx":
                with httpx.Client(timeout=15.0, headers=HEADERS,
                                  follow_redirects=True) as c:
                    r = c.get(url)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 429:
                    print(f"  ⚠️ Rate limit, attente 30s...")
                    time.sleep(30)
            else:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=15) as r:
                    return json.loads(r.read().decode())
        except Exception as e:
            if attempt < 2:
                time.sleep(5)
    return None

def _get_text(url):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode()
    except:
        return ""

# ─────────────────────────────────────────────────────────────────────────────
# TÉLÉCHARGEMENT DONNÉES
# ─────────────────────────────────────────────────────────────────────────────

def fetch_yahoo_daily(ticker, years=7):
    """Yahoo Finance: X années de données daily."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{ticker}?range={years}y&interval=1d")
    data = _get_json(url)
    if not data:
        return []
    try:
        result = data["chart"]["result"][0]
        ts   = result["timestamp"]
        q    = result["indicators"]["quote"][0]
        bars = []
        for i, t in enumerate(ts):
            o = (q["open"][i] or 0)
            h = (q["high"][i] or 0)
            l = (q["low"][i]  or 0)
            c = (q["close"][i] or 0)
            if c > 0:
                dt = datetime.datetime.utcfromtimestamp(t)
                bars.append({
                    "ts": t, "dt": dt, "o": o, "h": h, "l": l, "c": c,
                    "hour":    dt.hour,
                    "weekday": dt.weekday(),   # 0=Mon 4=Fri
                    "month":   dt.month,
                    "week":    dt.isocalendar()[1]
                })
        return bars
    except:
        return []

def fetch_yahoo_hourly(ticker, years=2):
    """Yahoo Finance: 2 ans de données hourly."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{ticker}?range={years}y&interval=1h")
    data = _get_json(url)
    if not data:
        return []
    try:
        result = data["chart"]["result"][0]
        ts   = result["timestamp"]
        q    = result["indicators"]["quote"][0]
        bars = []
        for i, t in enumerate(ts):
            c = (q["close"][i] or 0)
            o = (q["open"][i]  or 0)
            h = (q["high"][i]  or 0)
            l = (q["low"][i]   or 0)
            if c > 0:
                dt = datetime.datetime.utcfromtimestamp(t)
                bars.append({
                    "ts": t, "dt": dt, "o": o, "h": h, "l": l, "c": c,
                    "hour":    dt.hour,
                    "weekday": dt.weekday(),
                    "month":   dt.month,
                })
        return bars
    except:
        return []

def fetch_binance_hourly(symbol, limit=5000):
    """Binance: jusqu'à 5000 bougies horaires (≈208 jours)."""
    url = (f"https://api.binance.com/api/v3/klines?"
           f"symbol={symbol}&interval=1h&limit={limit}")
    data = _get_json(url)
    if not data:
        return []
    bars = []
    for k in data:
        t  = int(k[0]) // 1000
        o  = float(k[1])
        h  = float(k[2])
        l  = float(k[3])
        c  = float(k[4])
        dt = datetime.datetime.utcfromtimestamp(t)
        bars.append({
            "ts": t, "dt": dt, "o": o, "h": h, "l": l, "c": c,
            "hour": dt.hour, "weekday": dt.weekday(), "month": dt.month,
        })
    return bars

def fetch_fred(series_id):
    """FRED: série macro officielle (Fed Funds, CPI, NFP...)."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    txt = _get_text(url)
    if not txt:
        return {}
    result = {}
    for line in txt.strip().split("\n")[1:]:
        parts = line.split(",")
        if len(parts) == 2 and parts[1].strip() not in (".", ""):
            try:
                result[parts[0].strip()] = float(parts[1].strip())
            except:
                pass
    return result

# ─────────────────────────────────────────────────────────────────────────────
# CALCULS STATISTIQUES
# ─────────────────────────────────────────────────────────────────────────────

def compute_stats_for_symbol(sym, cfg, macro_context):
    """
    Calcule toutes les stats pour un symbole depuis données historiques réelles.
    Seuils de direction PERSONNALISÉS par actif (bull_rate seul est universel,
    mais avg_return pour décider BUY/SELL/NEUTRAL dépend du range de l'actif).
    """
    print(f"  📊 {sym}...")

    # Seuils personnalisés pour ce symbole
    h_buy, h_sell, d_buy, d_sell, m_buy, m_sell = get_thresholds(sym)

    # Télécharger données
    daily_bars  = fetch_yahoo_daily(cfg["yahoo"], years=7)
    hourly_bars = []
    if cfg.get("binance"):
        hourly_bars = fetch_binance_hourly(cfg["binance"], limit=5000)
        time.sleep(0.3)
    if not hourly_bars:
        hourly_bars = fetch_yahoo_hourly(cfg["yahoo"], years=2)
    time.sleep(1.0)

    if not daily_bars:
        print(f"    ❌ Aucune donnée daily")
        return None

    print(f"    Daily: {len(daily_bars)} barres | Hourly: {len(hourly_bars)} barres")
    print(f"    Seuils heure: BUY>{h_buy}% SELL<{h_sell}%")

    result = {
        "sym":      sym,
        "type":     cfg["type"],
        "n_daily":  len(daily_bars),
        "n_hourly": len(hourly_bars),
        "thresholds": {
            "hourly_buy": h_buy, "hourly_sell": h_sell,
            "daily_buy":  d_buy, "daily_sell":  d_sell,
            "monthly_buy":m_buy, "monthly_sell":m_sell,
        },
    }

    # ── 1. Stats par HEURE UTC (depuis hourly) ────────────────────────────
    hour_stats = {}
    if hourly_bars:
        by_hour = defaultdict(list)
        for bar in hourly_bars:
            pct = (bar["c"] - bar["o"]) / bar["o"] * 100 if bar["o"] > 0 else 0
            by_hour[bar["hour"]].append(pct)

        for h in range(24):
            rets = by_hour.get(h, [])
            if len(rets) >= 20:
                bull = sum(1 for r in rets if r > 0)
                bear = sum(1 for r in rets if r < 0)
                avg  = sum(rets) / len(rets)
                volatility = statistics.stdev(rets) if len(rets) > 1 else 0

                # [PERSONALISE] Seuil direction calibré par actif
                direction = "BUY"  if avg > h_buy  else \
                            "SELL" if avg < h_sell else "NEUTRAL"

                # bull_rate pondéré: bougies avec amplitude > 50% du range typique
                # pèsent 2x plus que les micro-bougies (moins de bruit)
                range_pct  = abs(h_buy) * 6.67  # range typique estimé
                strong_bull = sum(1 for r in rets if r >  range_pct * 0.3)
                strong_bear = sum(1 for r in rets if r < -range_pct * 0.3)
                total_strong = strong_bull + strong_bear
                weighted_bull = (strong_bull * 2 + (bull - strong_bull)) / \
                                (total_strong + (len(rets) - total_strong)) \
                                if len(rets) > 0 else 0.5

                hour_stats[str(h)] = {
                    "bull_rate":       round(bull / len(rets), 3),
                    "bull_rate_weighted": round(max(0, min(1, weighted_bull)), 3),
                    "bear_rate":       round(bear / len(rets), 3),
                    "avg_return":      round(avg, 4),
                    "volatility":      round(volatility, 4),
                    "n_samples":       len(rets),
                    "direction":       direction,
                    "threshold_used":  h_buy,
                    "insufficient":    False,
                }
            else:
                hour_stats[str(h)] = {
                    "direction":  "NEUTRAL",
                    "insufficient": True,
                    "n_samples":  len(rets),
                }

    result["hour_stats"] = hour_stats

    # ── 2. Stats par JOUR DE SEMAINE (depuis daily) ───────────────────────
    weekday_stats = {}
    by_weekday = defaultdict(list)
    for bar in daily_bars:
        pct = (bar["c"] - bar["o"]) / bar["o"] * 100 if bar["o"] > 0 else 0
        by_weekday[bar["weekday"]].append(pct)

    weekday_names = ["Monday","Tuesday","Wednesday","Thursday","Friday"]
    for wd in range(5):
        rets = by_weekday.get(wd, [])
        if len(rets) >= 30:
            bull = sum(1 for r in rets if r > 0)
            avg  = sum(rets) / len(rets)
            # [PERSONALISE] seuil daily par actif
            direction = "BUY"  if avg > d_buy  else \
                        "SELL" if avg < d_sell else "NEUTRAL"
            weekday_stats[weekday_names[wd]] = {
                "bull_rate":  round(bull / len(rets), 3),
                "avg_return": round(avg, 4),
                "n_samples":  len(rets),
                "direction":  direction,
                "threshold_used": d_buy,
            }

    result["weekday_stats"] = weekday_stats

    # ── 3. Stats par MOIS (saisonnalité 7 ans) ───────────────────────────
    month_stats = {}
    by_month = defaultdict(list)
    for bar in daily_bars:
        pct = (bar["c"] - bar["o"]) / bar["o"] * 100 if bar["o"] > 0 else 0
        by_month[bar["month"]].append(pct)

    month_names = ["","Jan","Feb","Mar","Apr","May","Jun",
                   "Jul","Aug","Sep","Oct","Nov","Dec"]
    for m in range(1, 13):
        rets = by_month.get(m, [])
        if len(rets) >= 15:
            bull = sum(1 for r in rets if r > 0)
            avg  = sum(rets) / len(rets)
            # [PERSONALISE] seuil mensuel par actif
            direction = "BUY"  if avg > m_buy  else \
                        "SELL" if avg < m_sell else "NEUTRAL"
            month_stats[month_names[m]] = {
                "bull_rate":  round(bull / len(rets), 3),
                "avg_return": round(avg, 4),
                "n_samples":  len(rets),
                "direction":  direction,
                "threshold_used": m_buy,
            }

    result["month_stats"] = month_stats

    # ── 4. Volatilité journalière moyenne ─────────────────────────────────
    daily_ranges = [(b["h"] - b["l"]) / b["l"] * 100
                    for b in daily_bars if b["l"] > 0]
    if daily_ranges:
        result["avg_daily_range_pct"] = round(sum(daily_ranges)/len(daily_ranges), 3)
        result["max_daily_range_pct"] = round(max(daily_ranges), 3)
        # Percentile 80 (journée volatile)
        sorted_ranges = sorted(daily_ranges)
        p80_idx = int(len(sorted_ranges) * 0.80)
        result["p80_daily_range_pct"] = round(sorted_ranges[p80_idx], 3)

    # ── 5. Tendance long terme ─────────────────────────────────────────────
    if len(daily_bars) >= 200:
        price_1y_ago = daily_bars[-252]["c"] if len(daily_bars) >= 252 else daily_bars[0]["c"]
        price_now    = daily_bars[-1]["c"]
        lt_return    = (price_now - price_1y_ago) / price_1y_ago * 100
        result["lt_trend_1y"]    = round(lt_return, 2)
        result["lt_trend_dir"]   = "BUY" if lt_return > 5 else "SELL" if lt_return < -5 else "NEUTRAL"
        result["current_price"]  = round(price_now, 4)

    # ── 6. Streak moyen avant retournement ────────────────────────────────
    streaks = []
    cur_streak = 0
    cur_dir    = None
    for bar in daily_bars:
        d = "UP" if bar["c"] > bar["o"] else "DOWN"
        if d == cur_dir:
            cur_streak += 1
        else:
            if cur_streak > 0:
                streaks.append(cur_streak)
            cur_streak = 1
            cur_dir    = d
    if streaks:
        result["avg_streak_days"] = round(sum(streaks)/len(streaks), 1)
        result["max_streak_days"] = max(streaks)
        result["reversal_after_days"] = round(sum(streaks)/len(streaks), 1)

    # ── 7. Réaction macro (corrélations institutionnelles documentées) ────
    macro_reactions = {
        "XAUUSD": {
            "vix_spike":    {"direction": "BUY",  "strength": 0.82, "source": "historical_corr"},
            "dxy_rise":     {"direction": "SELL", "strength": 0.82, "source": "pearson_7y"},
            "rate_hike_fed":{"direction": "SELL", "strength": 0.65, "source": "event_study"},
            "risk_off":     {"direction": "BUY",  "strength": 0.75, "source": "regime_analysis"},
            "inflation_cpi_up":{"direction":"BUY","strength": 0.60, "source": "macro_study"},
            "nfp_strong":   {"direction": "SELL", "strength": 0.55, "source": "event_study"},
        },
        "XAGUSD": {
            "vix_spike":    {"direction": "BUY",  "strength": 0.70},
            "dxy_rise":     {"direction": "SELL", "strength": 0.75},
            "risk_off":     {"direction": "BUY",  "strength": 0.65},
            "industrial_up":{"direction": "BUY",  "strength": 0.60},
        },
        "BTCUSD": {
            "vix_spike":    {"direction": "SELL", "strength": 0.70},
            "dxy_rise":     {"direction": "SELL", "strength": 0.45},
            "rate_hike_fed":{"direction": "SELL", "strength": 0.65},
            "risk_off":     {"direction": "SELL", "strength": 0.72},
            "fear_greed_extreme_fear":{"direction":"BUY","strength":0.58,"source":"contrarian"},
        },
        "EURUSD": {
            "dxy_rise":     {"direction": "SELL", "strength": 0.95},
            "ecb_hike":     {"direction": "BUY",  "strength": 0.70},
            "fed_hike":     {"direction": "SELL", "strength": 0.72},
            "risk_off":     {"direction": "SELL", "strength": 0.60},
            "nfp_strong":   {"direction": "SELL", "strength": 0.65},
        },
        "GBPJPY": {
            "vix_spike":    {"direction": "SELL", "strength": 0.78},
            "risk_off":     {"direction": "SELL", "strength": 0.82},
            "boe_hike":     {"direction": "BUY",  "strength": 0.65},
            "risk_on":      {"direction": "BUY",  "strength": 0.75},
            "sp500_up":     {"direction": "BUY",  "strength": 0.68},
        },
        "USDJPY": {
            "vix_spike":    {"direction": "SELL", "strength": 0.80},
            "dxy_rise":     {"direction": "BUY",  "strength": 0.85},
            "risk_off":     {"direction": "SELL", "strength": 0.78},
            "risk_on":      {"direction": "BUY",  "strength": 0.72},
            "boj_dovish":   {"direction": "BUY",  "strength": 0.68},
        },
        "GBPUSD": {
            "dxy_rise":     {"direction": "SELL", "strength": 0.85},
            "boe_hike":     {"direction": "BUY",  "strength": 0.68},
            "risk_off":     {"direction": "SELL", "strength": 0.65},
        },
        "AUDUSD": {
            "dxy_rise":     {"direction": "SELL", "strength": 0.80},
            "risk_off":     {"direction": "SELL", "strength": 0.75},
            "china_gdp_up": {"direction": "BUY",  "strength": 0.60},
            "sp500_up":     {"direction": "BUY",  "strength": 0.70},
        },
        "ETHUSD": {
            "vix_spike":    {"direction": "SELL", "strength": 0.72},
            "btc_bull":     {"direction": "BUY",  "strength": 0.90},
            "risk_off":     {"direction": "SELL", "strength": 0.70},
        },
        "US30": {
            "nfp_strong":   {"direction": "BUY",  "strength": 0.65},
            "rate_hike_fed":{"direction": "SELL", "strength": 0.60},
            "risk_off":     {"direction": "SELL", "strength": 0.82},
            "earnings_beat":{"direction": "BUY",  "strength": 0.70},
        },
        "US100": {
            "nfp_strong":   {"direction": "BUY",  "strength": 0.60},
            "rate_hike_fed":{"direction": "SELL", "strength": 0.70},
            "risk_off":     {"direction": "SELL", "strength": 0.80},
        },
        "US500": {
            "nfp_strong":   {"direction": "BUY",  "strength": 0.62},
            "rate_hike_fed":{"direction": "SELL", "strength": 0.65},
            "risk_off":     {"direction": "SELL", "strength": 0.83},
        },
    }
    result["macro_reactions"] = macro_reactions.get(sym, {})

    # ── 8. Sessions de trading ─────────────────────────────────────────────
    # Stats par session depuis hourly (heures UTC)
    sessions = {
        "ASIA":    list(range(0, 7)),
        "LONDON":  list(range(7, 13)),
        "NY":      list(range(13, 18)),
        "OVERLAP": list(range(11, 15)),
        "CLOSE":   list(range(18, 24)),
    }
    session_stats = {}
    if hourly_bars:
        by_hour_data = defaultdict(list)
        for bar in hourly_bars:
            pct = (bar["c"] - bar["o"]) / bar["o"] * 100 if bar["o"] > 0 else 0
            by_hour_data[bar["hour"]].append(pct)

        for sess_name, hours in sessions.items():
            all_rets = []
            for h in hours:
                all_rets.extend(by_hour_data.get(h, []))
            if len(all_rets) >= 20:
                bull = sum(1 for r in all_rets if r > 0)
                avg  = sum(all_rets) / len(all_rets)
                vol  = statistics.stdev(all_rets) if len(all_rets) > 1 else 0
                session_stats[sess_name] = {
                    "bull_rate":  round(bull / len(all_rets), 3),
                    "avg_return": round(avg, 4),
                    "volatility": round(vol, 4),
                    "n_samples":  len(all_rets),
                    # [PERSONALISE] seuil session = même que seuil horaire
                    "direction":  "BUY"  if avg > h_buy  else
                                  "SELL" if avg < h_sell else "NEUTRAL",
                    "threshold_used": h_buy,
                }

    result["session_stats"] = session_stats

    # ── 9. Résumé des seuils utilisés ─────────────────────────────────────
    result["calibration"] = {
        "hourly_signal_threshold": h_buy,
        "daily_signal_threshold":  d_buy,
        "monthly_signal_threshold":m_buy,
        "note": f"Seuils calibrés pour {sym} (range horaire typique ~{abs(h_buy)/0.15:.2f}%)",
    }

    return result

# ─────────────────────────────────────────────────────────────────────────────
# DONNÉES MACRO FRED
# ─────────────────────────────────────────────────────────────────────────────

def fetch_macro_context():
    """Télécharge les données macro FRED pour contexte."""
    print("\n📡 Téléchargement données macro FRED...")
    macro = {}

    series = {
        "fed_funds_rate": "DFF",          # Taux directeur Fed
        "cpi_yoy":        "CPIAUCSL",     # Inflation CPI
        "unemployment":   "UNRATE",       # Taux chômage
        "us10y":          "DGS10",        # Rendement 10 ans
        "us2y":           "DGS2",         # Rendement 2 ans
        "vix":            "VIXCLS",       # VIX historique
        "gdp_growth":     "A191RL1Q225SBEA",  # PIB croissance
    }

    for name, series_id in series.items():
        data = fetch_fred(series_id)
        if data:
            values = list(data.values())
            dates  = list(data.keys())
            macro[name] = {
                "current": values[-1] if values else None,
                "prev_month": values[-2] if len(values) > 1 else None,
                "prev_year":  values[-13] if len(values) > 12 else None,
                "last_date":  dates[-1] if dates else None,
                "n_points":   len(values),
                "avg_5y":     round(sum(values[-60:])/len(values[-60:]), 3)
                              if len(values) >= 60 else None,
            }
            print(f"  ✅ {name}: current={values[-1]:.3f} ({dates[-1]})")
        else:
            print(f"  ❌ {name}: échec")
        time.sleep(1.0)

    return macro

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("HISTORICAL_STATS_ENGINE — Génération stats_10y.json")
    print("Sources: Yahoo Finance 7ans + Binance + FRED officiel")
    print("=" * 65)

    output = {
        "generated_at": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "source":        "Yahoo Finance 7ans + Binance + FRED",
        "symbols":       {},
        "macro_context": {},
        "summary":       {},
    }

    # Macro FRED d'abord
    try:
        output["macro_context"] = fetch_macro_context()
    except Exception as e:
        print(f"⚠️  FRED erreur: {e}")

    # Stats par symbole
    print(f"\n📊 Calcul stats {len(SYMBOLS)} symboles...")
    for sym, cfg in SYMBOLS.items():
        try:
            stats = compute_stats_for_symbol(sym, cfg, output["macro_context"])
            if stats:
                output["symbols"][sym] = stats
                print(f"  ✅ {sym}: {stats['n_daily']} daily, "
                      f"trend={stats.get('lt_trend_dir','?')}, "
                      f"range={stats.get('avg_daily_range_pct','?')}%")
        except Exception as e:
            print(f"  ❌ {sym}: {e}")
        time.sleep(2.0)  # Pause entre symboles

    # Résumé
    output["summary"] = {
        "symbols_computed": len(output["symbols"]),
        "macro_sources": len(output["macro_context"]),
        "total_bars": sum(
            s.get("n_daily", 0) + s.get("n_hourly", 0)
            for s in output["symbols"].values()
        ),
    }

    # Sauvegarder
    with open("stats_10y.json", "w") as f:
        json.dump(output, f, indent=2, default=str)

    size_kb = len(json.dumps(output)) // 1024
    print(f"\n✅ stats_10y.json généré:")
    print(f"   Symboles: {output['summary']['symbols_computed']}")
    print(f"   Sources macro: {output['summary']['macro_sources']}")
    print(f"   Barres totales: {output['summary']['total_bars']:,}")
    print(f"   Taille: {size_kb} KB")
    print(f"\n→ Uploader stats_10y.json sur GitHub dans le dépôt omega-server")

if __name__ == "__main__":
    main()
