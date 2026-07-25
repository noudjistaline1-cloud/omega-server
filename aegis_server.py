"""
AEGIS SERVER v3 — Backend décisionnel du système de trading AEGIS.
Système NEUF, indépendant de STALINE.

NOUVEAU EN v3 (complétion des deux points laissés ouverts en v2):

  1) FILTRE CALENDRIER ÉCONOMIQUE (via FRED, l'API déjà utilisée sur STALINE)
     - Récupère les dates de publication de séries FRED configurées (NFP, CPI, etc.)
     - Bloque le trading FOREX/METAL le jour d'une publication à fort impact
     - LIMITE HONNÊTE: l'API FRED "release dates" donne une DATE, pas une heure
       précise. Le filtre bloque donc la journée entière de publication, pas une
       fenêtre de 30 minutes. C'est un choix assumé de robustesse plutôt que de
       prétendre une précision que l'API ne fournit pas.
     - Dégradation propre: si pas de clé FRED configurée ou API injoignable,
       le filtre ne bloque rien (log un avertissement une fois), il ne fait
       jamais planter le système.

  2) MODÈLE ML AUTO-ENTRAÎNÉ (régression logistique en ligne, SGD)
     - En plus des poids de scoring "perceptron-like" de la v2 (gardés),
       un vrai modèle probabiliste apprend en continu: P(trade gagnant | features)
     - Entraîné à chaque /trade_result (aucun besoin de dataset historique,
       il démarre neutre à 50% et s'affine trade après trade)
     - Sert de second filtre: si la probabilité prédite est trop basse, le
       signal est refusé même si le score par règles était bon
     - Sert aussi à moduler la taille de position (plus confiant = plus gros,
       dans les limites du risque autorisé)
     - Poids + biais persistés sur disque, aucune perte au redémarrage

Le reste (v1+v2) est inchangé : universel par % de risque, multi-timeframe,
régime de volatilité, garde-fou d'exposition, boucle de feedback, sécurité,
robustesse. Voir README.md pour le détail complet.
"""

import json
import logging
import math
import os
import secrets
import time
import urllib.request
import urllib.error
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone, date
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("AEGIS_DATA_DIR", "./aegis_data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = DATA_DIR / "state.json"
LOG_FILE = DATA_DIR / "aegis.log"

API_KEY = os.environ.get("AEGIS_API_KEY", "CHANGE_ME_BEFORE_DEPLOY")

MAX_REQUESTS_PER_MIN = 120
DAILY_LOSS_HALT_PERCENT = float(os.environ.get("AEGIS_DAILY_LOSS_HALT_PCT", "3.0"))
DEFAULT_RISK_PERCENT = float(os.environ.get("AEGIS_DEFAULT_RISK_PCT", "1.0"))
MAX_GROUP_EXPOSURE = int(os.environ.get("AEGIS_MAX_GROUP_EXPOSURE", "2"))

LEARNING_RATE = float(os.environ.get("AEGIS_LEARNING_RATE", "0.05"))
WEIGHT_MIN, WEIGHT_MAX = 0.05, 1.5
PENDING_SIGNAL_TTL_SECONDS = 3600 * 24 * 3

SCORE_THRESHOLD = 0.35

# --- v3: calendrier économique (FRED) ---
FRED_API_KEY = os.environ.get("AEGIS_FRED_API_KEY", "")
# IDs de "release" FRED à surveiller, ex: "10,50" — à trouver sur fred.stlouisfed.org/releases
# (chaque release a un ID visible dans l'URL de sa page). Laissé vide par défaut car les IDs
# doivent être vérifiés par l'utilisateur plutôt que devinés.
FRED_RELEASE_IDS = [r.strip() for r in os.environ.get("AEGIS_FRED_RELEASE_IDS", "").split(",") if r.strip()]
FRED_BLOCK_GROUPS = set(g.strip().upper() for g in os.environ.get("AEGIS_CALENDAR_GROUPS", "FOREX,METAL").split(","))
CALENDAR_LOOKAHEAD_DAYS = 7
CALENDAR_CACHE_REFRESH_HOURS = 12

# --- v3: modèle ML en ligne ---
ML_LEARNING_RATE = float(os.environ.get("AEGIS_ML_LEARNING_RATE", "0.10"))
ML_MIN_TRAIN_SAMPLES = int(os.environ.get("AEGIS_ML_MIN_SAMPLES", "20"))  # avant d'activer le gate
ML_MIN_PROBABILITY = float(os.environ.get("AEGIS_ML_MIN_PROBABILITY", "0.40"))
ML_FEATURES = ["trend", "momentum", "htf", "atr_dev", "spread_ratio"]

# --------------------------------------------------------------------------
# LOGGING
# --------------------------------------------------------------------------

logger = logging.getLogger("aegis")
logger.setLevel(logging.INFO)
_handler = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=5)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_handler)
_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_console)


def mask(s: str) -> str:
    if not s or len(s) < 6:
        return "***"
    return s[:3] + "..." + s[-2:]


# --------------------------------------------------------------------------
# GROUPES D'ACTIFS
# --------------------------------------------------------------------------

def get_asset_group(symbol: str) -> str:
    s = symbol.upper()
    if any(k in s for k in ("BTC", "ETH", "LTC", "XRP", "SOL", "DOGE")):
        return "CRYPTO"
    if any(k in s for k in ("XAU", "XAG", "GOLD", "SILVER")):
        return "METAL"
    return "FOREX"


# --------------------------------------------------------------------------
# CALENDRIER ÉCONOMIQUE (FRED)
# --------------------------------------------------------------------------

_calendar_cache: dict[str, list[str]] = {}   # release_id -> ["YYYY-MM-DD", ...]
_calendar_last_refresh: float = 0.0
_calendar_warned = False


def refresh_calendar_cache(force: bool = False):
    global _calendar_last_refresh, _calendar_warned
    now = time.time()
    if not force and (now - _calendar_last_refresh) < CALENDAR_CACHE_REFRESH_HOURS * 3600:
        return
    if not FRED_API_KEY or not FRED_RELEASE_IDS:
        if not _calendar_warned:
            logger.info("Filtre calendrier désactivé (pas de AEGIS_FRED_API_KEY / AEGIS_FRED_RELEASE_IDS configurés)")
            _calendar_warned = True
        return

    today = date.today().isoformat()
    for rid in FRED_RELEASE_IDS:
        try:
            url = (
                "https://api.stlouisfed.org/fred/release/dates"
                f"?release_id={rid}&api_key={FRED_API_KEY}&file_type=json"
                f"&realtime_start={today}&sort_order=asc&limit=10"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "AEGIS/3.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            dates = [d["date"] for d in data.get("release_dates", [])]
            _calendar_cache[rid] = dates
            logger.info(f"Calendrier FRED release_id={rid} rafraîchi: {len(dates)} date(s) à venir")
        except Exception as e:
            logger.warning(f"Échec rafraîchissement calendrier FRED release_id={rid}: {e}")
            # on garde le cache précédent (s'il existe) plutôt que de tout bloquer/débloquer
    _calendar_last_refresh = now


def is_calendar_blocked(symbol: str) -> tuple[bool, str]:
    group = get_asset_group(symbol)
    if group not in FRED_BLOCK_GROUPS:
        return False, ""
    if not FRED_API_KEY or not FRED_RELEASE_IDS:
        return False, ""

    refresh_calendar_cache(force=False)
    today = date.today().isoformat()
    for rid, dates in _calendar_cache.items():
        if today in dates:
            return True, f"CALENDAR_BLOCK_release_{rid}_{today}"
    return False, ""


# --------------------------------------------------------------------------
# ÉTAT PERSISTANT
# --------------------------------------------------------------------------

DEFAULT_WEIGHTS = {"trend": 0.6, "momentum": 0.4, "htf": 0.3}


def sigmoid(z: float) -> float:
    z = max(-35.0, min(35.0, z))
    return 1.0 / (1.0 + math.exp(-z))


class AegisState:
    def __init__(self):
        self.kill_switch: bool = False
        self.circuit_breaker_tripped: bool = False
        self.day_start_equity: dict[str, float] = {}
        self.day_date: str = ""
        self.last_error: Optional[str] = None
        self.started_at: str = datetime.now(timezone.utc).isoformat()

        # poids "perceptron-like" (v2)
        self.weights: dict[str, float] = dict(DEFAULT_WEIGHTS)
        self.weight_updates_count: int = 0
        self.wins: int = 0
        self.losses: int = 0

        # exposition (v2)
        self.exposure: dict[str, dict[str, int]] = {}

        # --- v3 : modèle ML en ligne (régression logistique) ---
        self.ml_weights: dict[str, float] = {f: 0.0 for f in ML_FEATURES}
        self.ml_bias: float = 0.0
        self.ml_updates_count: int = 0

        self._load()

    def _load(self):
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                self.kill_switch = data.get("kill_switch", False)
                self.circuit_breaker_tripped = data.get("circuit_breaker_tripped", False)
                self.day_start_equity = data.get("day_start_equity", {})
                self.day_date = data.get("day_date", "")
                self.weights = data.get("weights", dict(DEFAULT_WEIGHTS))
                self.weight_updates_count = data.get("weight_updates_count", 0)
                self.wins = data.get("wins", 0)
                self.losses = data.get("losses", 0)
                self.exposure = data.get("exposure", {})
                self.ml_weights = data.get("ml_weights", {f: 0.0 for f in ML_FEATURES})
                self.ml_bias = data.get("ml_bias", 0.0)
                self.ml_updates_count = data.get("ml_updates_count", 0)
            except Exception as e:
                logger.error(f"Échec chargement état, on repart propre: {e}")

    def save(self):
        tmp = STATE_FILE.with_suffix(".tmp")
        payload = {
            "kill_switch": self.kill_switch,
            "circuit_breaker_tripped": self.circuit_breaker_tripped,
            "day_start_equity": self.day_start_equity,
            "day_date": self.day_date,
            "weights": self.weights,
            "weight_updates_count": self.weight_updates_count,
            "wins": self.wins,
            "losses": self.losses,
            "exposure": self.exposure,
            "ml_weights": self.ml_weights,
            "ml_bias": self.ml_bias,
            "ml_updates_count": self.ml_updates_count,
        }
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(STATE_FILE)

    def check_new_day(self, account_id: str, equity: float):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.day_date:
            self.day_date = today
            self.day_start_equity = {}
            self.circuit_breaker_tripped = False
            logger.info("Nouveau jour UTC -> reset circuit breaker + baseline equity")
        if account_id not in self.day_start_equity:
            self.day_start_equity[account_id] = equity
        self.save()

    def evaluate_circuit_breaker(self, account_id: str, equity: float) -> bool:
        baseline = self.day_start_equity.get(account_id)
        if not baseline or baseline <= 0:
            return False
        drawdown_pct = (baseline - equity) / baseline * 100.0
        if drawdown_pct >= DAILY_LOSS_HALT_PERCENT:
            if not self.circuit_breaker_tripped:
                logger.warning(
                    f"CIRCUIT BREAKER déclenché — compte {account_id}: "
                    f"drawdown {drawdown_pct:.2f}% >= seuil {DAILY_LOSS_HALT_PERCENT}%"
                )
            self.circuit_breaker_tripped = True
            self.save()
        return self.circuit_breaker_tripped

    # ---- exposition ----
    def get_exposure(self, account_id: str, group: str) -> int:
        return self.exposure.get(account_id, {}).get(group, 0)

    def increment_exposure(self, account_id: str, group: str):
        self.exposure.setdefault(account_id, {})
        self.exposure[account_id][group] = self.exposure[account_id].get(group, 0) + 1
        self.save()

    def decrement_exposure(self, account_id: str, group: str):
        if account_id in self.exposure and group in self.exposure[account_id]:
            self.exposure[account_id][group] = max(0, self.exposure[account_id][group] - 1)
            self.save()

    # ---- apprentissage "perceptron-like" (v2, conservé) ----
    def update_weights(self, features: dict, outcome_sign: int):
        for key in ("trend", "momentum", "htf"):
            val = features.get(key, 0.0)
            self.weights[key] += LEARNING_RATE * outcome_sign * val
            self.weights[key] = max(WEIGHT_MIN, min(WEIGHT_MAX, self.weights[key]))
        self.weight_updates_count += 1
        if outcome_sign > 0:
            self.wins += 1
        elif outcome_sign < 0:
            self.losses += 1
        self.save()
        logger.info(
            f"Poids règles mis à jour (#{self.weight_updates_count}) outcome={outcome_sign} "
            f"-> trend={self.weights['trend']:.3f} momentum={self.weights['momentum']:.3f} "
            f"htf={self.weights['htf']:.3f} | W:{self.wins} L:{self.losses}"
        )

    # ---- v3 : modèle ML en ligne (régression logistique, SGD) ----
    def ml_predict(self, features: dict) -> float:
        z = self.ml_bias
        for f in ML_FEATURES:
            z += self.ml_weights.get(f, 0.0) * features.get(f, 0.0)
        return sigmoid(z)

    def ml_update(self, features: dict, label: int):
        """label: 1 = trade gagnant, 0 = trade perdant. SGD sur la log-loss."""
        p = self.ml_predict(features)
        grad = (label - p)  # dérivée de la log-loss par rapport à z
        for f in ML_FEATURES:
            x = features.get(f, 0.0)
            self.ml_weights[f] = self.ml_weights.get(f, 0.0) + ML_LEARNING_RATE * grad * x
        self.ml_bias += ML_LEARNING_RATE * grad
        self.ml_updates_count += 1
        self.save()
        logger.info(
            f"Modèle ML mis à jour (#{self.ml_updates_count}) label={label} p_avant={p:.3f} "
            f"-> weights={ {k: round(v,3) for k,v in self.ml_weights.items()} } bias={self.ml_bias:.3f}"
        )


state = AegisState()

# --------------------------------------------------------------------------
# SIGNAUX EN ATTENTE
# --------------------------------------------------------------------------

pending_signals: dict[str, dict] = {}


def cleanup_pending_signals():
    now = time.time()
    expired = [sid for sid, v in pending_signals.items() if now - v["ts"] > PENDING_SIGNAL_TTL_SECONDS]
    for sid in expired:
        entry = pending_signals.pop(sid, None)
        if entry:
            state.decrement_exposure(entry["account_id"], entry["group"])
            logger.info(f"Signal {sid} expiré (jamais de résultat rapporté) -> exposition libérée")


# --------------------------------------------------------------------------
# RATE LIMITING
# --------------------------------------------------------------------------

_request_log: dict[str, deque] = defaultdict(deque)


def check_rate_limit(ip: str):
    now = time.time()
    dq = _request_log[ip]
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= MAX_REQUESTS_PER_MIN:
        raise HTTPException(status_code=429, detail="Rate limit dépassé")
    dq.append(now)


# --------------------------------------------------------------------------
# MODÈLES
# --------------------------------------------------------------------------

class SignalRequest(BaseModel):
    account_id: str = Field(..., min_length=1, max_length=64)
    symbol: str = Field(..., min_length=3, max_length=20)
    equity: float = Field(..., gt=0)
    balance: float = Field(..., gt=0)
    spread_points: float = Field(..., ge=0)
    ema_fast: float
    ema_slow: float
    ema_fast_h4: Optional[float] = None
    ema_slow_h4: Optional[float] = None
    rsi: float = Field(..., ge=0, le=100)
    atr: float = Field(..., gt=0)
    atr_long: Optional[float] = Field(default=None, gt=0)
    price: float = Field(..., gt=0)
    tick_value: float = Field(..., gt=0)
    lot_min: float = Field(..., gt=0)
    lot_max: float = Field(..., gt=0)
    lot_step: float = Field(..., gt=0)
    risk_percent: Optional[float] = Field(default=None, gt=0, le=10)

    @field_validator("symbol")
    @classmethod
    def upper_symbol(cls, v: str) -> str:
        return v.strip().upper()


class SignalResponse(BaseModel):
    action: Literal["BUY", "SELL", "FLAT"]
    score: float
    confidence: float
    ml_probability: Optional[float] = None
    lot: float
    sl_points: float
    tp_points: float
    reason: str
    halted: bool = False
    signal_id: Optional[str] = None


class KillSwitchRequest(BaseModel):
    engage: bool


class TradeResultRequest(BaseModel):
    account_id: str = Field(..., min_length=1, max_length=64)
    signal_id: str = Field(..., min_length=1, max_length=64)
    profit: float


# --------------------------------------------------------------------------
# INTELLIGENCE — SCORING MULTI-FACTEURS + FEATURES ML
# --------------------------------------------------------------------------

def _normalize(diff_ratio: float, scale: float = 50.0) -> float:
    return max(-1.0, min(1.0, diff_ratio * scale))


def compute_features(req: SignalRequest) -> dict:
    """Calcule TOUTES les features utilisées à la fois par le scoring à règles
    (v2) et par le modèle ML (v3), pour garantir la cohérence entre les deux."""
    features = {}

    trend = 0.0
    if req.ema_slow > 0:
        trend = _normalize((req.ema_fast - req.ema_slow) / req.ema_slow)
    features["trend"] = trend

    momentum = (req.rsi - 50.0) / 50.0
    features["momentum"] = momentum

    htf = 0.0
    if req.ema_fast_h4 is not None and req.ema_slow_h4 and req.ema_slow_h4 > 0:
        htf = _normalize((req.ema_fast_h4 - req.ema_slow_h4) / req.ema_slow_h4)
    features["htf"] = htf

    # Déviation du régime de volatilité (0 = normal, >0 = extrême)
    atr_dev = 0.0
    if req.atr_long and req.atr_long > 0:
        ratio = req.atr / req.atr_long
        atr_dev = max(-1.0, min(1.0, (ratio - 1.0)))
    features["atr_dev"] = atr_dev

    # Coût relatif du spread par rapport à l'ATR
    spread_ratio = 0.0
    if req.atr > 0:
        raw = req.spread_points / (req.atr * 100000) if req.atr < 1 else req.spread_points / req.atr
        spread_ratio = max(0.0, min(1.0, raw))
    features["spread_ratio"] = spread_ratio

    return features


def score_signal(req: SignalRequest, features: dict) -> tuple[float, str]:
    reasons = []
    w = state.weights
    weight_sum = max(0.01, w["trend"] + w["momentum"] + w["htf"])
    weighted = (w["trend"] * features["trend"] + w["momentum"] * features["momentum"]
                + w["htf"] * features["htf"]) / weight_sum

    reasons.append(f"trend={features['trend']:+.2f}")
    reasons.append(f"momentum={features['momentum']:+.2f}")
    reasons.append(f"htf={features['htf']:+.2f}")

    spread_penalty = -0.2 if features["spread_ratio"] > 0.3 else 0.0
    if spread_penalty:
        reasons.append("spread_eleve")

    score = max(-1.0, min(1.0, weighted + spread_penalty))
    return score, ", ".join(reasons)


def volatility_regime_scale(atr_dev: float) -> tuple[float, str]:
    if abs(atr_dev) >= 0.8:  # correspond à un ratio ATR court/long > 1.8 ou < 0.5 environ (mêmes seuils)
        return 0.5, f"vol_regime=extreme({atr_dev:+.2f})"
    return 1.0, f"vol_regime=normal({atr_dev:+.2f})"


def compute_universal_lot(equity: float, risk_percent: float, sl_points: float,
                            tick_value: float, lot_min: float, lot_max: float,
                            lot_step: float) -> float:
    if sl_points <= 0 or tick_value <= 0:
        return lot_min
    risk_amount = equity * (risk_percent / 100.0)
    raw_lot = risk_amount / (sl_points * tick_value)
    steps = round(raw_lot / lot_step)
    lot = steps * lot_step
    lot = max(lot_min, min(lot_max, lot))
    return round(lot, 8)


# --------------------------------------------------------------------------
# APP
# --------------------------------------------------------------------------

app = FastAPI(title="AEGIS Trading Backend", version="3.0.0")


def require_api_key(x_api_key: Optional[str] = Header(default=None)):
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        logger.warning("Tentative d'accès avec clé API invalide")
        raise HTTPException(status_code=401, detail="Clé API invalide")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    state.last_error = f"{type(exc).__name__}: {exc}"
    logger.error(f"Exception non gérée sur {request.url.path}: {exc}")
    return JSONResponse(status_code=500, content={"detail": "Erreur interne serveur, voir logs."})


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "3.0.0",
        "started_at": state.started_at,
        "circuit_breaker_tripped": state.circuit_breaker_tripped,
        "kill_switch": state.kill_switch,
        "last_error": state.last_error,
        "calendar_enabled": bool(FRED_API_KEY and FRED_RELEASE_IDS),
    }


@app.get("/stats")
def stats(x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    cleanup_pending_signals()
    total = state.wins + state.losses
    winrate = (state.wins / total * 100.0) if total > 0 else None
    return {
        "weights": state.weights,
        "weight_updates_count": state.weight_updates_count,
        "wins": state.wins,
        "losses": state.losses,
        "winrate_pct": round(winrate, 2) if winrate is not None else None,
        "exposure": state.exposure,
        "pending_signals_count": len(pending_signals),
        "circuit_breaker_tripped": state.circuit_breaker_tripped,
        "kill_switch": state.kill_switch,
        "day_start_equity": state.day_start_equity,
        "ml_weights": state.ml_weights,
        "ml_bias": state.ml_bias,
        "ml_updates_count": state.ml_updates_count,
        "ml_gate_active": state.ml_updates_count >= ML_MIN_TRAIN_SAMPLES,
        "calendar_enabled": bool(FRED_API_KEY and FRED_RELEASE_IDS),
        "calendar_cache": _calendar_cache,
    }


@app.post("/signal", response_model=SignalResponse)
def get_signal(req: SignalRequest, request: Request, x_api_key: Optional[str] = Header(default=None)):
    check_rate_limit(request.client.host if request.client else "unknown")
    require_api_key(x_api_key)
    cleanup_pending_signals()

    state.check_new_day(req.account_id, req.equity)

    if state.kill_switch:
        return SignalResponse(action="FLAT", score=0, confidence=0, lot=0, sl_points=0,
                               tp_points=0, reason="KILL_SWITCH_ENGAGED", halted=True)

    if state.evaluate_circuit_breaker(req.account_id, req.equity):
        return SignalResponse(action="FLAT", score=0, confidence=0, lot=0, sl_points=0,
                               tp_points=0, reason="CIRCUIT_BREAKER_DAILY_LOSS", halted=True)

    calendar_blocked, calendar_reason = is_calendar_blocked(req.symbol)
    if calendar_blocked:
        return SignalResponse(action="FLAT", score=0, confidence=0, lot=0, sl_points=0,
                               tp_points=0, reason=calendar_reason, halted=False)

    features = compute_features(req)
    score, reason = score_signal(req, features)
    confidence = abs(score)
    ml_p = state.ml_predict(features)

    if score >= SCORE_THRESHOLD:
        action = "BUY"
    elif score <= -SCORE_THRESHOLD:
        action = "SELL"
    else:
        action = "FLAT"

    # Gate ML : n'entre en action qu'une fois assez de trades appris, pour éviter
    # de bloquer sur un modèle encore quasi vierge (p toujours proche de 0.5)
    ml_gate_active = state.ml_updates_count >= ML_MIN_TRAIN_SAMPLES
    if action != "FLAT" and ml_gate_active and ml_p < ML_MIN_PROBABILITY:
        logger.info(f"[{req.account_id}] {req.symbol} signal {action} bloqué par le modèle ML (p={ml_p:.2f})")
        return SignalResponse(action="FLAT", score=round(score, 4), confidence=round(confidence, 4),
                               ml_probability=round(ml_p, 4), lot=0, sl_points=0, tp_points=0,
                               reason=f"ML_LOW_PROBABILITY(p={ml_p:.2f})", halted=False)

    group = get_asset_group(req.symbol)
    if action != "FLAT" and state.get_exposure(req.account_id, group) >= MAX_GROUP_EXPOSURE:
        logger.info(f"[{req.account_id}] {req.symbol} signal {action} bloqué: exposition {group} au max")
        return SignalResponse(action="FLAT", score=round(score, 4), confidence=round(confidence, 4),
                               ml_probability=round(ml_p, 4), lot=0, sl_points=0, tp_points=0,
                               reason=f"EXPOSURE_LIMIT_{group}", halted=False)

    vol_scale, vol_reason = volatility_regime_scale(features["atr_dev"])
    # Modulation supplémentaire par la confiance du modèle ML (si entraîné) :
    # 0.5 <-> 1.5x le risque de base selon p, borné pour rester sûr même sans historique
    ml_scale = 1.0
    if ml_gate_active:
        ml_scale = max(0.5, min(1.5, 0.5 + ml_p))
    reason = f"{reason}, {vol_reason}, ml_p={ml_p:.2f}"

    sl_points = max(req.atr * 150.0, 50.0)
    tp_points = sl_points * 1.8

    risk_percent = (req.risk_percent or DEFAULT_RISK_PERCENT) * vol_scale * ml_scale
    lot = 0.0
    signal_id = None

    if action != "FLAT":
        lot = compute_universal_lot(
            equity=req.equity, risk_percent=risk_percent, sl_points=sl_points,
            tick_value=req.tick_value, lot_min=req.lot_min, lot_max=req.lot_max,
            lot_step=req.lot_step,
        )
        signal_id = uuid.uuid4().hex
        pending_signals[signal_id] = {
            "account_id": req.account_id,
            "symbol": req.symbol,
            "group": group,
            "features": features,
            "ts": time.time(),
        }
        state.increment_exposure(req.account_id, group)

    logger.info(
        f"[{req.account_id}] {req.symbol} score={score:.2f} ml_p={ml_p:.2f} action={action} "
        f"lot={lot} equity={req.equity:.2f} risk%={risk_percent:.2f} sig={signal_id}"
    )

    return SignalResponse(
        action=action, score=round(score, 4), confidence=round(confidence, 4),
        ml_probability=round(ml_p, 4), lot=lot, sl_points=round(sl_points, 1),
        tp_points=round(tp_points, 1), reason=reason, halted=False, signal_id=signal_id,
    )


@app.post("/trade_result")
def trade_result(req: TradeResultRequest, x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)

    entry = pending_signals.pop(req.signal_id, None)
    if not entry:
        logger.warning(f"trade_result reçu pour signal_id inconnu/expiré: {req.signal_id}")
        return {"status": "unknown_signal_id", "learned": False}

    state.decrement_exposure(entry["account_id"], entry["group"])

    outcome_sign = 1 if req.profit > 0 else (-1 if req.profit < 0 else 0)
    if outcome_sign != 0:
        state.update_weights(entry["features"], outcome_sign)
        label = 1 if req.profit > 0 else 0
        state.ml_update(entry["features"], label)

    logger.info(f"[{req.account_id}] résultat trade {entry['symbol']} profit={req.profit:.2f} -> appris (règles + ML)")
    return {"status": "ok", "learned": outcome_sign != 0}


@app.post("/calendar/refresh")
def calendar_refresh(x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    refresh_calendar_cache(force=True)
    return {"calendar_cache": _calendar_cache}


@app.post("/killswitch")
def kill_switch(req: KillSwitchRequest, x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    state.kill_switch = req.engage
    state.save()
    logger.warning(f"KILL SWITCH -> {'ENGAGED' if req.engage else 'DISENGAGED'} (manuel)")
    return {"kill_switch": state.kill_switch}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    logger.info(f"AEGIS backend v3 démarrage sur le port {port} (clé API: {mask(API_KEY)})")
    refresh_calendar_cache(force=True)
    uvicorn.run(app, host="0.0.0.0", port=port)
