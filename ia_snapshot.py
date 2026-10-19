# -*- coding: utf-8 -*-
"""
[JOURNAL-IA] Instantané des données mondiales pour l'EA STALINE — GET /ia/snapshot

Sources publiques, sans clé API :
  • Yahoo Finance (5 min) : dollar DXY, VIX, MOVE, volatilité de l'or GVZ, taux US 10 ans / 3 mois,
    contrats S&P / Nasdaq / pétrole / argent / cuivre / or / obligations 10 ans, yen, euro, yuan, bitcoin
  • ForexFactory (calendrier de la semaine) : dernière annonce USD forte (réel / prévu / précédent → surprise)
    et prochaine annonce forte (minutes restantes)
  • GDELT 2.0 (toutes les 15 min) : intensité des conflits / violences / menaces dans le monde, ton des actualités
  • Policy Uncertainty (quotidien) : indice d'incertitude politique US
  • CFTC (hebdomadaire) : positionnement net des fonds spéculatifs sur l'or

Rien n'est bloquant : chaque source est rafraîchie en tâche de fond ; une source en panne laisse ses
champs vides (null) sans gêner les autres. La réponse est un JSON PLAT (clé → nombre) que l'EA lit
avec son extracteur existant. Aucun ordre, aucune décision : uniquement de l'observation.
"""
import asyncio, csv, io, math, time, zipfile, datetime as dt
import httpx
from fastapi import APIRouter

router = APIRouter()
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
_cache = {"data": {}, "ts": 0.0, "busy": False}
_slow = {"epu": (None, 0.0), "cot": (None, 0.0)}
TTL = 240

YAHOO = {
    "dxy": "DX-Y.NYB", "vix": "^VIX", "move": "^MOVE", "gvz": "^GVZ", "us10y": "^TNX", "us3m": "^IRX",
    "spx_fut": "ES=F", "ndx_fut": "NQ=F", "oil": "CL=F", "silver": "SI=F", "copper": "HG=F", "gold_fut": "GC=F",
    "bond10_fut": "ZN=F", "usdjpy": "USDJPY=X", "eurusd": "EURUSD=X", "usdcnh": "USDCNH=X", "btc": "BTC-USD",
}
LEVELS = {"vix", "move", "gvz", "us10y", "us3m"}   # on garde aussi le niveau


def _f(x):
    try:
        x = float(x)
        return None if (math.isnan(x) or math.isinf(x)) else round(x, 6)
    except Exception:
        return None


async def _yahoo(cl, key, sym):
    out = {}
    try:
        r = await cl.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}", params={"range": "5d", "interval": "5m"}, timeout=8)
        res = r.json()["chart"]["result"][0]
        c = [v for v in res["indicators"]["quote"][0]["close"] if v is not None]
        if len(c) >= 2:
            last = c[-1]
            out[f"{key}_last"] = _f(last)
            for n, lab in ((3, "r15"), (12, "r60")):
                if len(c) > n and c[-1 - n]:
                    out[f"{key}_{lab}"] = _f((last / c[-1 - n] - 1) * 100)          # en %
            if key in LEVELS:
                out[key] = _f(last)
    except Exception:
        pass
    return out


def _num(s):
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    mult = 1.0
    if s.endswith("%"):
        s = s[:-1]
    elif s[-1:] in "KMBT" and s[:-1]:
        mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[s[-1]]; s = s[:-1]
    try:
        return float(s) * mult
    except Exception:
        return None


async def _calendar(cl):
    out = {}
    try:
        r = await cl.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=8)
        ev = [e for e in r.json() if e.get("country") == "USD" and e.get("impact") == "High"]
        now = dt.datetime.now(dt.timezone.utc)
        past, fut = [], []
        for e in ev:
            try:
                t = dt.datetime.fromisoformat(e["date"]).astimezone(dt.timezone.utc)
            except Exception:
                continue
            (past if t <= now else fut).append((t, e))
        if past:
            t, e = max(past, key=lambda z: z[0])
            a, f, p = _num(e.get("actual")), _num(e.get("forecast")), _num(e.get("previous"))
            out["cal_last_min_ago"] = _f((now - t).total_seconds() / 60)
            out["cal_last_actual"], out["cal_last_forecast"], out["cal_last_previous"] = _f(a), _f(f), _f(p)
            if a is not None and f is not None:
                out["cal_last_surprise"] = _f(a - f)
                den = max(abs(f), abs(p) if p is not None else 0.0, 1e-9)
                out["cal_last_surprise_rel"] = _f((a - f) / den)
            out["cal_last_title"] = e.get("title", "")
        if fut:
            t, e = min(fut, key=lambda z: z[0])
            out["cal_next_min"] = _f((t - now).total_seconds() / 60)
            out["cal_next_title"] = e.get("title", "")
        out["cal_high_today"] = sum(1 for t, e in past + fut if t.date() == now.date())
    except Exception:
        pass
    return out


KEY = {"USA", "CHN", "RUS", "IRN", "ISR", "UKR", "SAU", "PRK", "TWN", "IND", "PAK", "TUR", "VEN", "PSE", "LBN", "YEM", "SYR", "IRQ"}


async def _gdelt(cl):
    out = {}
    try:
        lu = (await cl.get("http://data.gdeltproject.org/gdeltv2/lastupdate.txt", timeout=8)).text.split()
        url = next(x for x in lu if x.endswith(".export.CSV.zip"))
        b = (await cl.get(url, timeout=15)).content
        z = zipfile.ZipFile(io.BytesIO(b))
        rd = csv.reader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf8", errors="ignore"), delimiter="\t")
        n = q4 = viol = thr = key4 = 0; tone = 0.0; gs = 0.0
        for row in rd:
            try:
                root = int(row[28]); qc = int(row[29]); g = float(row[30]); t = float(row[34])
            except Exception:
                continue
            n += 1; tone += t; gs += g
            if qc == 4: q4 += 1
            if root >= 18: viol += 1
            if root in (13, 15, 17): thr += 1
            if qc == 4 and (row[7] in KEY or row[17] in KEY): key4 += 1
        if n:
            out.update(gd_events=n, gd_conflict_sh=_f(q4 / n), gd_violence_sh=_f(viol / n), gd_threat_sh=_f(thr / n),
                       gd_keyconflict=key4, gd_tone=_f(tone / n), gd_goldstein=_f(gs / n))
    except Exception:
        pass
    return out


async def _epu(cl):
    v, t = _slow["epu"]
    if v is not None and time.time() - t < 6 * 3600:
        return v
    out = {}
    try:
        r = await cl.get("https://www.policyuncertainty.com/media/All_Daily_Policy_Data.csv", timeout=15)
        rows = [l for l in r.text.strip().splitlines()[1:] if l.count(",") >= 3]
        vals = [float(l.split(",")[3]) for l in rows[-60:]]
        out["epu"] = _f(vals[-1])
        m = sum(vals) / len(vals); sd = (sum((x - m) ** 2 for x in vals) / len(vals)) ** 0.5
        out["epu_z60"] = _f((vals[-1] - m) / sd) if sd > 0 else None
    except Exception:
        pass
    _slow["epu"] = (out, time.time())
    return out


async def _cot(cl):
    v, t = _slow["cot"]
    if v is not None and time.time() - t < 12 * 3600:
        return v
    out = {}
    try:
        y = dt.date.today().year
        r = await cl.get(f"https://www.cftc.gov/files/dea/history/fut_disagg_txt_{y}.zip", timeout=30)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        rd = csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf8", errors="ignore"))
        best = {}
        for row in rd:
            if "GOLD - COMMODITY EXCHANGE" not in row.get("Market_and_Exchange_Names", ""):
                continue
            d = row["Report_Date_as_YYYY-MM-DD"]; oi = float(row["Open_Interest_All"])
            if d not in best or oi > best[d][0]:
                best[d] = (oi, float(row["M_Money_Positions_Long_All"]) - float(row["M_Money_Positions_Short_All"]))
        ds = sorted(best)
        if ds:
            oi, net = best[ds[-1]]
            out.update(cot_gold_mm_net_oi=_f(net / oi), cot_report_date=ds[-1])
            if len(ds) > 1:
                oi0, net0 = best[ds[-2]]
                out["cot_gold_mm_change_oi"] = _f(net / oi - net0 / oi0)
    except Exception:
        pass
    _slow["cot"] = (out, time.time())
    return out


async def _refresh():
    _cache["busy"] = True
    try:
        async with httpx.AsyncClient(headers=UA, follow_redirects=True, http2=False) as cl:
            parts = await asyncio.gather(*[_yahoo(cl, k, s) for k, s in YAHOO.items()], _calendar(cl), _gdelt(cl), _epu(cl), _cot(cl),
                                         return_exceptions=True)
        d = {}
        for p in parts:
            if isinstance(p, dict):
                d.update(p)
        # écart or / dollar sur 60 min (l'or monte quand le dollar baisse : un écart signale un mouvement propre à l'or)
        if d.get("gold_fut_r60") is not None and d.get("dxy_r60") is not None:
            d["gold_vs_dxy_r60"] = _f(d["gold_fut_r60"] + 4.0 * d["dxy_r60"])
        d["ts_utc"] = int(time.time())
        _cache["data"], _cache["ts"] = d, time.time()
    finally:
        _cache["busy"] = False


@router.get("/ia/snapshot")
async def ia_snapshot():
    if time.time() - _cache["ts"] > TTL and not _cache["busy"]:
        if not _cache["data"]:
            try:
                await asyncio.wait_for(_refresh(), timeout=25)   # premier appel : on attend une fois
            except Exception:
                pass
        else:
            asyncio.create_task(_refresh())                     # ensuite : rafraîchi en fond, réponse immédiate
    out = dict(_cache["data"])
    out["age_s"] = int(time.time() - _cache["ts"]) if _cache["ts"] else -1
    return out
