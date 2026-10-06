"""MARKET_MEMORY — mémoire statistique du comportement RÉEL du marché + décision finale à 3 sources.

Données : market_memory.json (construit à partir d'OHLC H1 réelles 2010-2026, crypto 2017-2026, heures UTC).
Principe validé hors échantillon (labo STALINE, oct. 2026) :
  * aucun effet heure / jour ne prédit SEUL la direction hors échantillon → la mémoire ne bloque jamais
    et ne choisit pas seule le sens ; elle MODULE le lot et la confiance ;
  * sur les trades TENDANCE (XAU/BTC/ETH), lot = 1 + 0,20·mémoire + 0,10·macro (borné 0,6-1,4) :
    choix 2010-21, 2022-26 hors échantillon : gain +14 %, rapport gain/creux inchangé ou meilleur.
Décision finale : p_final = 0,5 + 0,70·(p_marché_actuel − 0,5) + 0,20·(p_mémoire − 0,5) + 0,10·(p_macro − 0,5).
Action toujours « TRADE » : une source défavorable réduit le lot, elle n'interdit pas.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from typing import Any, Dict, Optional

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_memory.json")
_MEM: Dict[str, Any] = {}
_LOCK = threading.Lock()
W_CURRENT, W_MEMORY, W_MACRO = 0.70, 0.20, 0.10
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ALIASES = {"GOLD": "XAUUSD", "SILVER": "XAGUSD", "BITCOIN": "BTCUSD", "ETHEREUM": "ETHUSD", "XBTUSD": "BTCUSD",
           "BTCUSDT": "BTCUSD", "ETHUSDT": "ETHUSD", "XRPUSDT": "XRPUSD", "DOGEUSDT": "DOGEUSD", "SPX500": "US500",
           "SP500": "US500", "USTEC": "NAS100", "NDX": "NAS100", "NAS": "NAS100", "US100": "NAS100"}
_YAHOO = {"EURUSD": "EURUSD=X", "US500": "%5EGSPC"}
_proxy_cache: Dict[str, Dict[str, Any]] = {}


def load(path: str = _PATH) -> bool:
    global _MEM
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        with _LOCK:
            _MEM = data
        return True
    except Exception:
        return False


def loaded() -> bool:
    return bool(_MEM.get("assets"))


def canonical(symbol: str) -> Optional[str]:
    """XAUUSDm, XAUUSD.r, #GOLD, BTCUSDT… → clé de la mémoire (None si inconnu)."""
    s = re.sub(r"[^A-Z0-9]", "", str(symbol).upper())
    assets = _MEM.get("assets", {})
    if s in assets:
        return s
    if s in ALIASES:
        return ALIASES[s]
    for k in sorted(list(assets) + list(ALIASES), key=len, reverse=True):   # préfixe le plus long (XAUUSDM → XAUUSD)
        if s.startswith(k):
            return ALIASES.get(k, k)
    return None


def memory_view(symbol: str, hour_utc: int, weekday: int) -> Dict[str, Any]:
    """Comportement historique à cette heure UTC et ce jour : p(hausse), n, intervalle, amplitude, score signé BUY."""
    key = canonical(symbol)
    if not key:
        return {"available": False, "reason": "actif absent de la mémoire"}
    a = _MEM["assets"][key]
    h = a["hours"].get(str(int(hour_utc) % 24), {})
    d = a["weekdays"].get(DAYS[int(weekday) % 7], {})
    ph = 0.5 if (not h or h.get("rollover")) else float(h["p"])
    pd = float(d.get("p", 0.5)) if d else 0.5
    mem_buy = (ph - 0.5) + (pd - 0.5)
    sd = float(a.get("mem_sd") or 0.03)
    return {
        "available": True, "symbol": key, "hour_utc": int(hour_utc) % 24, "weekday": DAYS[int(weekday) % 7],
        "p_up_hour": round(ph, 4), "n_hour": h.get("n", 0), "ci_hour": [h.get("lo"), h.get("hi")],
        "stable_hour": bool(h.get("stable", False)), "rollover_hour": bool(h.get("rollover", False)),
        "p_up_day": round(pd, 4), "n_day": d.get("n", 0), "stable_day": bool(d.get("stable", False)),
        "amplitude_hour_bp": h.get("amp_bp"),
        "p_memory_buy": round(min(0.65, max(0.35, 0.5 + mem_buy)), 4),
        "mem_z_buy": round(max(-3.0, min(3.0, mem_buy / sd)), 3),
        "period": a.get("period"),
    }


def _fetch_proxy(name: str, getter=None) -> Optional[float]:
    """Rendement 5 jours de bourse (EURUSD ou S&P 500) via Yahoo, en cache 1 h. None si indisponible."""
    c = _proxy_cache.get(name)
    if c and time.time() - c["t"] < 3600:
        return c["v"]
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{_YAHOO[name]}?range=1mo&interval=1d"
    try:
        if getter is None:
            import httpx
            r = httpx.get(url, timeout=6.0, headers={"User-Agent": "Mozilla/5.0"})
            js = r.json()
        else:
            js = getter(url)
        closes = [x for x in js["chart"]["result"][0]["indicators"]["quote"][0]["close"] if x]
        if len(closes) < 6:
            return None
        v = closes[-1] / closes[-6] - 1.0
        _proxy_cache[name] = {"t": time.time(), "v": v}
        return v
    except Exception:
        return None


def macro_view(symbol: str, ws_macro: Optional[float] = None, proxy_value: Optional[float] = None) -> Dict[str, Any]:
    """Macro à POIDS FAIBLE : proxy validé (dollar 5 j pour or/argent, S&P 500 5 j pour crypto/indices) + World Scanner."""
    key = canonical(symbol)
    a = _MEM.get("assets", {}).get(key or "", {})
    m = a.get("macro")
    parts, z = [], None
    if m:
        v = proxy_value if proxy_value is not None else _fetch_proxy(m["proxy"])
        if v is not None:
            z = max(-3.0, min(3.0, m["sign"] * v / float(m["sd"] or 0.01)))
            parts.append(f"{m['proxy']} 5 j {v * 100:+.2f} % → {z:+.2f} σ pour l'achat")
    ws_z = None
    if ws_macro is not None:
        ws_z = max(-2.0, min(2.0, 2.0 * float(ws_macro)))
        parts.append(f"World Scanner macro {float(ws_macro):+.2f}")
    if z is not None and ws_z is not None:
        comb = 0.7 * z + 0.3 * ws_z
    else:
        comb = z if z is not None else (ws_z if ws_z is not None else 0.0)
    return {"available": z is not None or ws_z is not None, "macro_z_buy": round(comb, 3),
            "p_macro_buy": round(0.5 + 0.05 * max(-2.0, min(2.0, comb)), 4),
            "proxy": m.get("proxy") if m else None, "detail": parts or ["aucune macro disponible → neutre"]}


def correlation_view(symbol: str, trade_dir: int, open_positions: Optional[str] = None) -> Dict[str, Any]:
    """Corrélations stables 2010-2026 (rendements journaliers). open_positions = "ETHUSD:BUY,XAUUSDm:SELL".
    Règles : confirmation BTC par ETH (validée) ; exposition corrélée ≥ 1,5 → lot ×0,8 (protection, non prédictive)."""
    key = canonical(symbol)
    row = (_MEM.get("assets", {}).get(key or "", {}) or {}).get("correlations", {})
    mult, notes, expo, eth_same = 1.0, [], 0.0, None
    pos = []
    for item in (open_positions or "").split(","):
        if ":" not in item:
            continue
        sy, dd = item.split(":", 1)
        ck, dv = canonical(sy.strip()), (1 if dd.strip().upper().startswith("B") else -1)
        if ck and ck != key:
            pos.append((ck, dv))
    for ck, dv in pos:
        c = row.get(ck)
        if c is None:
            continue
        if {key, ck} == {"BTCUSD", "ETHUSD"}:
            continue                                  # paire validée traitée à part
        expo += c * dv * trade_dir
    if key == "BTCUSD" and trade_dir != 0 and open_positions is not None:
        eth_same = any(ck == "ETHUSD" and dv == trade_dir for ck, dv in pos)
        mult *= 1.5 if eth_same else 0.75
        notes.append("BTC confirmé par une position ETH dans le même sens → ×1,5" if eth_same else "BTC seul (ETH pas parti dans ce sens) → ×0,75")
    if expo >= 1.5:
        mult *= 0.8
        notes.append(f"exposition corrélée déjà forte dans ce sens ({expo:+.2f}) → ×0,8")
    return {"correlated_assets": row, "open_positions_used": [f"{a}:{'BUY' if d > 0 else 'SELL'}" for a, d in pos],
            "exposure_same_direction": round(expo, 3), "btc_eth_confirmed": eth_same, "corr_mult": round(mult, 3),
            "notes": notes or ["aucune règle de corrélation déclenchée"]}


def decide(symbol: str, direction: Optional[str] = None, p_current: Optional[float] = None,
           hour_utc: Optional[int] = None, weekday: Optional[int] = None, ws_macro: Optional[float] = None,
           proxy_value: Optional[float] = None, price: Optional[float] = None,
           open_positions: Optional[str] = None) -> Dict[str, Any]:
    """Décision finale à 3 sources. Ne renvoie JAMAIS d'interdiction : seulement sens, probabilité et multiplicateur de lot."""
    now = time.gmtime()
    hour_utc = now.tm_hour if hour_utc is None or hour_utc < 0 else int(hour_utc)
    weekday = now.tm_wday if weekday is None or weekday < 0 else int(weekday)
    d_in = (direction or "").upper()
    if p_current is None:
        p_current = 0.5 + (0.1 if d_in == "BUY" else -0.1 if d_in == "SELL" else 0.0)
    p_current = max(0.0, min(1.0, float(p_current)))
    mem = memory_view(symbol, hour_utc, weekday) if loaded() else {"available": False}
    mac = macro_view(symbol, ws_macro, proxy_value) if loaded() else {"available": False, "macro_z_buy": 0.0, "p_macro_buy": 0.5}
    p_mem = mem.get("p_memory_buy", 0.5) if mem.get("available") else 0.5
    p_mac = mac.get("p_macro_buy", 0.5)
    p_final = 0.5 + W_CURRENT * (p_current - 0.5) + W_MEMORY * (p_mem - 0.5) + W_MACRO * (p_mac - 0.5)
    final_dir = "BUY" if p_final > 0.5 else "SELL" if p_final < 0.5 else (d_in or "NEUTRAL")
    s = 1.0 if final_dir == "BUY" else -1.0
    lf = _MEM.get("lot_formula", {"k_memory": 0.2, "k_macro": 0.1, "min": 0.6, "max": 1.4})
    mz = mem.get("mem_z_buy", 0.0) if mem.get("available") else 0.0
    cz = mac.get("macro_z_buy", 0.0)
    lot = max(lf["min"], min(lf["max"], 1.0 + lf["k_memory"] * mz * s + lf["k_macro"] * cz * s))
    cor = correlation_view(symbol, 1 if final_dir == "BUY" else -1, open_positions) if loaded() else {"corr_mult": 1.0, "correlated_assets": {}}
    lot_base = lot
    lot = lot * cor["corr_mult"]
    agree_mem = "neutre" if abs(mz) < 0.3 else ("d'accord" if mz * s > 0 else "contre")
    agree_mac = "neutre" if abs(cz) < 0.3 else ("d'accord" if cz * s > 0 else "contre")
    conflict = []
    if d_in in ("BUY", "SELL") and final_dir != d_in:
        conflict.append(f"sens demandé {d_in} mais la fusion donne {final_dir} (marché actuel trop faible face à la mémoire/macro)")
    if agree_mem == "contre":
        conflict.append("mémoire contre → lot réduit")
    if agree_mac == "contre":
        conflict.append("macro contre → lot légèrement réduit")
    out = {
        "symbol": symbol, "memory_symbol": mem.get("symbol"), "action": "TRADE", "blocked": False,
        "direction": final_dir, "p_buy": round(p_final, 4), "p_sell": round(1 - p_final, 4),
        "confidence": round(abs(p_final - 0.5) * 2, 4), "lot_mult": round(lot, 3), "lot_mult_memory_macro": round(lot_base, 3),
        "weights": {"current_market": W_CURRENT, "market_memory": W_MEMORY, "macro": W_MACRO},
        "decision_1_current_market": {"p_buy": round(p_current, 4), "direction_requested": d_in or None},
        "decision_2_market_memory": mem | {"agreement": agree_mem},
        "decision_3_macro": mac | {"agreement": agree_mac},
        "decision_4_correlations": cor,
        "conflicts": conflict,
        "note": "La mémoire et la macro modulent le lot ; elles n'interdisent jamais un trade.",
    }
    for k_, v_ in (cor.get("correlated_assets") or {}).items():
        out["corr_" + k_] = v_                          # clés plates, lisibles par l'EA
    if price and mem.get("amplitude_hour_bp"):
        out["protection_hint"] = {
            "amplitude_hour_price": round(float(price) * float(mem["amplitude_hour_bp"]) / 1e4, 5),
            "rule": "ne pas sécuriser avant ~1 amplitude horaire typique de mouvement favorable (laisser respirer)"}
    return out


def hist_score_for(symbol: str, req_dir: str, hour_utc: int, weekday: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Remplace la source P2 de /score (constante 0,50 à cause d'un mauvais nom de clé) par la vraie mémoire."""
    if not loaded():
        return None
    if weekday is None:
        weekday = time.gmtime().tm_wday
    mv = memory_view(symbol, hour_utc, weekday)
    if not mv.get("available"):
        return None
    p = mv["p_memory_buy"] if req_dir == "BUY" else 1.0 - mv["p_memory_buy"]
    return {"score": round(p, 4), "direction": "BUY" if mv["p_memory_buy"] > 0.5 else "SELL" if mv["p_memory_buy"] < 0.5 else "NEUTRAL",
            "available": True, "n": mv["n_hour"], "source": "MARKET_MEMORY_2010_2026",
            "note": f"h{mv['hour_utc']} p_up={mv['p_up_hour']} {mv['weekday']} p_up={mv['p_up_day']}"}


load()
