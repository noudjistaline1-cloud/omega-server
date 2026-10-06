import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import MARKET_MEMORY as M

def test_loaded_and_aliases():
    assert M.loaded()
    assert M.canonical("XAUUSDm") == "XAUUSD"
    assert M.canonical("BTCUSDT") == "BTCUSD"
    assert M.canonical("#GOLD") == "XAUUSD"
    assert M.canonical("USTEC") == "NAS100"
    assert M.canonical("FOOBAR") is None

def test_never_blocks_and_bounds():
    for sym in ["XAUUSD", "BTCUSD", "EURUSD", "UNKNOWN"]:
        for d, p in [("BUY", 0.9), ("SELL", 0.1), ("BUY", 0.5), ("", None)]:
            r = M.decide(sym, d, p, 14, 2, 0.8, 0.05)
            assert r["action"] == "TRADE" and r["blocked"] is False
            assert 0.6 <= r["lot_mult"] <= 1.4

def test_current_market_dominates():
    r = M.decide("XAUUSD", "SELL", 0.35, 14, 2, 1.0, 0.10)   # macro très haussière, marché actuel vendeur
    assert r["direction"] == "SELL"
    assert r["lot_mult"] < 1.0                                # la macro contre réduit le lot, n'inverse pas

def test_rollover_hour_neutral_for_fx():
    v = M.memory_view("EURUSD", 22, 1)
    assert v["rollover_hour"] and v["p_up_hour"] == 0.5

def test_hist_score_for():
    r = M.hist_score_for("XAUUSD", "BUY", 14, 2)
    assert r and 0.3 < r["score"] < 0.7 and r["n"] > 1000
