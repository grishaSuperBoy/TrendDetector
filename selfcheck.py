"""
selfcheck.py — обязательная проверка после ЛЮБОЙ правки кода.

    python selfcheck.py            прогнать все проверки
    python selfcheck.py --seal     если все PASS — обновить MANIFEST.sha256

Синтетика, временные папки. Данные не трогаются.
"""
import dataclasses
import json
import logging
import os
import shutil
import sys
import tempfile
import types

import collector_settings as CS
import toxic_scanner as TS

logging.basicConfig(level=logging.CRITICAL)
RESULTS = []


def check(name, cond, info=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS  " if cond else "FAIL  ") + name + (f"   [{info}]" if (info and not cond) else ""))


# ============================================================ settings
def test_settings():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "s.json")
    json.dump({"toxic_alert": -5, "obi_vel_min": "abc", "bogus": 1,
               "toxic_min_vol_usd": 1_000_000, "strict_integrity": "yes",
               "breadth_move_pp": 200, "breadth_hot_min": 7}, open(p, "w"))
    s = CS.load_settings(p)
    check("settings: мусор отвергается",
          s.toxic_alert == 0.70 and s.obi_vel_min == 0.05 and s.strict_integrity is False)
    check("settings: корректные значения принимаются",
          s.toxic_min_vol_usd == 1_000_000 and s.breadth_hot_min == 7)
    check("settings: вне диапазона — дефолт",
          s.breadth_move_pp == 40.0, s.breadth_move_pp)
    check("settings: oi_squeeze_max_drop_pct дефолт -1.0",
          s.oi_squeeze_max_drop_pct == -1.0)
    open(p, "w").write("{не json")
    check("settings: битый JSON не роняет загрузку", CS.load_settings(p) == CS.Settings())
    shutil.rmtree(d, ignore_errors=True)


# ============================================================ toxic_scanner
def test_toxic_80_20():
    """80% buy / 20% sell → toxic_1h ≈ 0.6"""
    S = CS.Settings(toxic_min_vol_usd=0.0)
    sc = TS.ToxicScanner(S)
    sym = "TOXUSDT"
    t0 = 1_000_000.0
    for m in range(60):
        ts = t0 + m * 60.0
        for _ in range(4):
            sc.tick("binance", sym, 100.0, 1000.0, False, ts)   # buy $100k
        sc.tick("binance", sym, 100.0, 1000.0, True, ts)        # sell $100k
    now = t0 + 60 * 60.0
    sc.recompute(lambda s, n: None, None, None, now)
    m = sc.metrics(sym)
    check("toxic: 80/20 → toxic_1h ≈ 0.6", m and abs(m["toxic_1h"] - 0.6) < 0.02,
          m["toxic_1h"] if m else None)
    check("toxic: flow_dir = +1", m and m["flow_dir"] == 1)
    check("toxic: vol_1h_usd > 0", m and m["vol_1h_usd"] > 0)


def test_toxic_50_50():
    S = CS.Settings(toxic_min_vol_usd=0.0)
    sc = TS.ToxicScanner(S)
    sym = "BALUSDT"
    t0 = 2_000_000.0
    for m in range(60):
        ts = t0 + m * 60.0
        for _ in range(5):
            sc.tick("binance", sym, 100.0, 1000.0, False, ts)
        for _ in range(5):
            sc.tick("binance", sym, 100.0, 1000.0, True, ts)
    now = t0 + 60 * 60.0
    sc.recompute(lambda s, n: None, None, None, now)
    m = sc.metrics(sym)
    check("toxic: 50/50 → ≈ 0", m and m["toxic_1h"] < 0.02, m["toxic_1h"] if m else None)


def test_toxic_buckets_cap():
    """Бакетов не больше 250."""
    S = CS.Settings()
    sc = TS.ToxicScanner(S)
    sym = "LONGUSDT"
    t0 = 3_000_000.0
    for m in range(500):
        sc.tick("binance", sym, 100.0, 100.0, False, t0 + m * 60.0)
    b = sc._buckets[("binance", sym)]
    check("toxic: бакетов ≤ 250", len(b) <= 250, len(b))


def test_toxic_slope():
    S = CS.Settings()
    sc = TS.ToxicScanner(S)
    sym = "SLOPEUSDT"
    # вручную положим историю
    from collections import deque
    sc._hist[sym] = deque([(1000.0, 0.10), (1100.0, 0.12), (1300.0, 0.20)], maxlen=30)
    slope = sc.toxic_slope(sym, window_sec=300.0)
    check("toxic_slope: растёт → > 0", slope > 0, slope)



# ============================================================ ignition
def test_ignition_check():
    S = CS.Settings()
    cases = [
        # toxic, dir, vol, ret, slope, br, ctx, obi_now, obi_vel, expected, comment
        (0.8, +1, 1_000_000, +3.0, 0.08, 10, "liq",  +0.3, +0.10, True,  "лонг всё сошлось"),
        (0.8, -1, 1_000_000, -3.0, 0.08, 10, "liq",  -0.3, -0.10, True,  "шорт всё сошлось"),
        (0.8, +1, 1_000_000, +7.0, 0.08, 10, "liq",  +0.3, +0.10, False, "цена > 6 bps"),
        (0.8, +1, 1_000_000, +3.0, 0.08, 10, "liq",  +0.3, +0.02, False, "OBI vel слабый"),
        (0.8, +1, 1_000_000, +3.0, 0.08, 10, "liq",  None, None,  False, "нет OBI"),
        (0.8, +1, 1_000_000, +3.0, 0.02, 10, "liq",  +0.3, +0.10, False, "slope слабый"),
        (0.8, +1, 1_000_000, +3.0, 0.08,  3, "liq",  +0.3, +0.10, False, "breadth слабый"),
        (0.8, +1, 1_000_000, +3.0, 0.08, 10, None,   +0.3, +0.10, False, "нет контекста"),
        (0.5, +1, 1_000_000, +3.0, 0.08, 10, "liq",  +0.3, +0.10, False, "toxic слабый"),
        (0.8,  0, 1_000_000, +3.0, 0.08, 10, "liq",  +0.3, +0.10, False, "нет направления"),
        (0.8, +1, 1_000_000, -3.0, 0.08, 10, "liq",  +0.3, +0.10, False, "цена против"),
    ]
    for toxic, d, vol, ret, sl, br, ctx, on, ov, exp, cmt in cases:
        got = TS.ignition_check(toxic, d, vol, ret, sl, br, ctx, on, ov, S)
        check(f"ignition_check: {cmt} → {expected_bool(exp)}", got is exp, f"got={got}")


def expected_bool(x):
    return x


# ============================================================ toxic_signals
def test_toxic_signals():
    S = CS.Settings(toxic_min_vol_usd=0.0)
    sc = TS.ToxicScanner(S)
    sym = "SIGUSDT"
    t0 = 4_000_000.0
    for m in range(60):
        ts = t0 + m * 60.0
        for _ in range(5):
            sc.tick("binance", sym, 100.0, 1000.0, False, ts)

    # первый recompute: ret=0 → accumulation (точнее acc_long, так как BUY)
    ts1 = t0 + 3600.0 + 10.0
    sc.recompute(lambda s, n: 0.0, lambda s, d: None, lambda s: (+0.5, +0.10), ts1)
    sigs1 = sc.take_signals()
    check("toxic_signals: первый сигнал", len(sigs1) == 1, len(sigs1))
    if sigs1:
        check("toxic_signals: verdict_new = acc_long",
              sigs1[0]["verdict_new"] == "acc_long", sigs1[0]["verdict_new"])

    # второй recompute без смены — сигналов нет
    ts2 = ts1 + 10.0
    sc.recompute(lambda s, n: 0.0, lambda s, d: None, lambda s: (+0.5, +0.10), ts2)
    check("toxic_signals: без смены — пусто", len(sc.take_signals()) == 0)


# ============================================================ breadth
def test_breadth():
    S = CS.Settings()
    sc = TS.ToxicScanner(S)
    # заполним метрики вручную
    sc._metrics = {
        "AUSDT": {"obi_now": +0.30, "obi_vel": +0.10, "coin_state": "trend_up"},
        "BUSDT": {"obi_now": +0.20, "obi_vel": +0.08, "coin_state": "flat"},
        "CUSDT": {"obi_now": -0.25, "obi_vel": -0.10, "coin_state": "acc_short"},
        "DUSDT": {"obi_now": +0.05, "obi_vel": +0.01, "coin_state": "flat"},
        "EUSDT": {"obi_now": None, "obi_vel": None, "coin_state": "flat"},
    }
    b = sc.breadth()
    check("breadth: long_pressure = 2", b["long_pressure"] == 2, b["long_pressure"])
    check("breadth: short_pressure = 1", b["short_pressure"] == 1, b["short_pressure"])
    check("breadth: trend_up = 1", b["state_counts"]["trend_up"] == 1, b["state_counts"].get("trend_up"))
    check("breadth: acc_short = 1", b["state_counts"]["acc_short"] == 1, b["state_counts"].get("acc_short"))
    # diff = (2-1)/5 * 100 = 20.0
    check("breadth: diff_pp = 20.0", abs(b["diff_pp"] - 20.0) < 0.1, b["diff_pp"])


# ============================================================ engine integration
def _fake_snap(sym, mid, obi=0.0, sp=2.0, ex="binance"):
    return types.SimpleNamespace(
        exchange=ex, symbol=sym, ts=1_000_000.0,
        mid_price=mid,
        best_bid=mid * (1 - sp / 2e4), best_ask=mid * (1 + sp / 2e4),
        spread_bps=sp, obi=obi,
        depth_bid_usd=5000.0, depth_ask_usd=5000.0,
        bids=[(mid * (1 - i * 1e-4), 10.0) for i in range(5)],
        asks=[(mid * (1 + i * 1e-4), 10.0) for i in range(5)],
    )


def test_engine_obi_pair():
    import engine as E
    clock = [1_000_000.0]
    real_time = E.time.time
    E.time = types.SimpleNamespace(time=lambda: clock[0])
    try:
        S = dataclasses.replace(CS.Settings(), obi_vel_window_sec=30.0)
        eng = E.OBIEngine(settings=S)
        # 30с тиков с растущим OBI
        for i in range(100):
            clock[0] += 0.4
            eng.on_depth_update(_fake_snap("BTCUSDT", 100.0, obi=0.05 + i * 0.003))
        obi_now, obi_vel = eng._obi_pair("BTCUSDT")
        check("obi_pair: obi_now > 0", obi_now is not None and obi_now > 0, obi_now)
        check("obi_pair: obi_vel > 0", obi_vel is not None and obi_vel > 0, obi_vel)
    finally:
        E.time = types.SimpleNamespace(time=real_time)


def test_engine_ret_30s():
    import engine as E
    clock = [1_000_000.0]
    real_time = E.time.time
    E.time = types.SimpleNamespace(time=lambda: clock[0])
    try:
        S = CS.Settings()
        eng = E.OBIEngine(settings=S)
        # 40с стабильной цены
        for i in range(100):
            clock[0] += 0.4
            eng.on_depth_update(_fake_snap("BTCUSDT", 100.0))
        # скачок +1%
        clock[0] += 0.5
        eng.on_depth_update(_fake_snap("BTCUSDT", 101.0))
        r = eng._ret_30s("BTCUSDT", clock[0])
        check("ret_30s: считает от старой точки", r is not None and abs(r - 100.0) < 10, r)
    finally:
        E.time = types.SimpleNamespace(time=real_time)


def test_engine_ignition_context():
    import engine as E
    from models import LiquidationEvent
    clock = [1_000_000.0]
    real_time = E.time.time
    E.time = types.SimpleNamespace(time=lambda: clock[0])
    try:
        S = dataclasses.replace(CS.Settings(),
                                ignition_context_liq_usd=50_000.0,
                                ignition_context_window_sec=5.0)
        eng = E.OBIEngine(settings=S)
        # нет ликвидаций — None
        ctx, val = eng._ignition_context("BTCUSDT", +1)
        check("ignition_context: без ливок → None", ctx is None)

        # есть ливка BUY на $120k
        eng.on_liquidation(LiquidationEvent(
            id="x", symbol="BTCUSDT", side="BUY", price=100.0,
            qty=1200.0, qty_usd=120_000.0, exchange="binance", ts=clock[0],
        ))
        ctx, val = eng._ignition_context("BTCUSDT", +1)
        check("ignition_context: ливка есть → liq", ctx == "liq", ctx)
        check("ignition_context: значение $120k", val == "$120k", val)

        # не та сторона — None
        ctx2, _ = eng._ignition_context("BTCUSDT", -1)
        check("ignition_context: не та сторона → None", ctx2 is None)

        # отключено (liq_usd=0) → контекст считается пройденным
        S2 = dataclasses.replace(CS.Settings(), ignition_context_liq_usd=0.0)
        eng2 = E.OBIEngine(settings=S2)
        ctx3, val3 = eng2._ignition_context("BTCUSDT", +1)
        check("ignition_context: отключено → liq/off", ctx3 == "liq" and val3 == "off", (ctx3, val3))
    finally:
        E.time = types.SimpleNamespace(time=real_time)


def test_engine_robustness():
    import engine as E
    S = CS.Settings()
    eng = E.OBIEngine(settings=S)
    raised = False
    try:
        eng.on_depth_update(None)
        eng.on_depth_update(types.SimpleNamespace(mid_price=float("nan"), exchange="x", symbol="y"))
        eng.on_agg_trade("A", float("nan"), 1, True)
        eng.on_liquidation(None)
    except Exception:
        raised = True
    check("robust: мусор не бросает", not raised)
    r = eng.extract_and_reset_unflushed()
    check("robust: выгрузка работает", isinstance(r, dict) and "toxic_signals" in r)


# ============================================================ integrity
def test_integrity():
    d = tempfile.mkdtemp()
    for n in CS.CORE_FILES:
        src = os.path.join(CS.HERE, n)
        if os.path.exists(src):
            shutil.copy(src, d)
    CS.seal_manifest(d)
    ok1, _ = CS.verify_integrity(d)
    with open(os.path.join(d, "engine.py"), "a") as f:
        f.write("\n# чужая правка\n")
    ok2, diffs = CS.verify_integrity(d)
    check("integrity: печать подтверждает код", ok1 is True)
    check("integrity: изменение обнаружено", ok2 is False and any("engine.py" in x for x in diffs))
    shutil.rmtree(d, ignore_errors=True)


# ============================================================ oi_tracker
def test_oi_tracker():
    from oi_tracker import OITracker
    tracker = OITracker()
    # 1. Пустая история -> (None, None)
    cur, tr = tracker.trend_15m("BTCUSDT")
    check("oi_tracker: пустая история → (None, None)", cur is None and tr is None)

    # 2. Падающая на 5% за 15 мин -> trend_15m ≈ -5
    t0 = 1_000_000.0
    tracker._fetch = lambda sym: 1000.0
    tracker.poll(["BTCUSDT"], now=t0)
    tracker._fetch = lambda sym: 950.0
    tracker.poll(["BTCUSDT"], now=t0 + 900.0)
    cur, tr = tracker.trend_15m("BTCUSDT", now=t0 + 900.0)
    check("oi_tracker: падающая на 5% за 15 мин → trend_15m ≈ -5",
          cur == 950.0 and tr is not None and abs(tr - (-5.0)) < 0.1, f"tr={tr}")

    # 3. Растущая на 5% за 15 мин -> trend_15m ≈ +5
    tracker2 = OITracker()
    tracker2._fetch = lambda sym: 1000.0
    tracker2.poll(["ETHUSDT"], now=t0)
    tracker2._fetch = lambda sym: 1050.0
    tracker2.poll(["ETHUSDT"], now=t0 + 900.0)
    cur2, tr2 = tracker2.trend_15m("ETHUSDT", now=t0 + 900.0)
    check("oi_tracker: растущая на 5% за 15 мин → trend_15m ≈ +5",
          cur2 == 1050.0 and tr2 is not None and abs(tr2 - 5.0) < 0.1, f"tr2={tr2}")


def test_states():
    from states import CoinState, MarketState, coin_state, market_state
    
    # coin_state: 7 states
    # flat (no vol/toxic)
    c = coin_state(0.1, 0, False, None, False, None, {"alert":0.7, "min_ret":4.0, "oi_drop":-1.0})
    check("coin_flat", c == CoinState.FLAT)
    
    # acc_long (toxic, dir=0 or not enough price/obi)
    c = coin_state(0.8, 1, True, 2.0, False, None, {"alert":0.7, "min_ret":4.0, "oi_drop":-1.0})
    check("coin_acc_long", c == CoinState.ACC_LONG)
    
    # acc_short
    c = coin_state(0.8, -1, True, -2.0, False, None, {"alert":0.7, "min_ret":4.0, "oi_drop":-1.0})
    check("coin_acc_short", c == CoinState.ACC_SHORT)
    
    # trend_up (toxic, flow>0, vol_ok, ret_ok, obi_ok, oi_trend >= -1)
    c = coin_state(0.8, 1, True, 5.0, True, 1.0, {"alert":0.7, "min_ret":4.0, "oi_drop":-1.0})
    check("coin_trend_up", c == CoinState.TREND_UP)
    
    # trend_down (toxic, flow<0, vol_ok, ret_ok, obi_ok, oi_trend >= -1)
    c = coin_state(0.8, -1, True, -5.0, True, 2.0, {"alert":0.7, "min_ret":4.0, "oi_drop":-1.0})
    check("coin_trend_down", c == CoinState.TREND_DOWN)
    
    # squeeze_up (trend_up conditions, but oi_trend < -1.0)
    c = coin_state(0.8, 1, True, 5.0, True, -5.0, {"alert":0.7, "min_ret":4.0, "oi_drop":-1.0})
    check("coin_squeeze_up", c == CoinState.SQUEEZE_UP)
    
    # squeeze_down
    c = coin_state(0.8, -1, True, -5.0, True, -3.0, {"alert":0.7, "min_ret":4.0, "oi_drop":-1.0})
    check("coin_squeeze_down", c == CoinState.SQUEEZE_DOWN)
    
    # market_state: 5 states
    p = {"trend_frac": 0.4, "squeeze_frac": 0.3}
    
    # CHOP: empty or all flat
    check("market_chop", market_state([CoinState.FLAT]*10, p) == MarketState.CHOP)
    
    # LONG_TREND: >= 40% TREND_UP
    st = [CoinState.TREND_UP]*4 + [CoinState.FLAT]*6
    check("market_long_trend", market_state(st, p) == MarketState.LONG_TREND)
    
    # SHORT_TREND: >= 40% TREND_DOWN
    st = [CoinState.TREND_DOWN]*5 + [CoinState.FLAT]*5
    check("market_short_trend", market_state(st, p) == MarketState.SHORT_TREND)
    
    # SQUEEZE_RISK: >= 30% squeeze (overrides trend)
    st = [CoinState.TREND_UP]*4 + [CoinState.SQUEEZE_UP]*3 + [CoinState.FLAT]*3
    check("market_squeeze", market_state(st, p) == MarketState.SQUEEZE_RISK)
    
    # MIXED: TREND_UP >= 40% AND TREND_DOWN >= 40% (edge case) or not enough trend/squeeze but not all flat
    st = [CoinState.TREND_UP]*4 + [CoinState.TREND_DOWN]*4 + [CoinState.FLAT]*2
    check("market_mixed", market_state(st, p) == MarketState.MIXED)


# ============================================================ main
def main():
    test_settings()
    test_toxic_80_20()
    test_toxic_50_50()
    test_toxic_buckets_cap()
    test_toxic_slope()
    test_ignition_check()
    test_toxic_signals()
    test_breadth()
    test_engine_obi_pair()
    test_engine_ret_30s()
    test_engine_ignition_context()
    test_engine_robustness()
    test_oi_tracker()
    test_states()
    test_integrity()

    bad = [n for n, ok in RESULTS if not ok]
    print("\n" + ("ВСЁ PASS (%d проверок)" % len(RESULTS) if not bad
                  else "ЕСТЬ ПРОВАЛЫ: " + "; ".join(bad)))
    if "--seal" in sys.argv:
        if bad:
            print("печать НЕ обновлена: есть провалы")
        else:
            print("печать обновлена:", CS.seal_manifest())
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
