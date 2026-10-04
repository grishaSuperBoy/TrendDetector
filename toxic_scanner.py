"""
toxic_scanner.py — токсичность потока (VPIN-лайт) + вердикты + ignition.

Идея: токсичность = |buy - sell| / (buy + sell) за окно.
0 — сбалансировано, 1 — односторонне.

Хранится не сырая лента за 4ч, а минутные бакеты:
    {(ex, sym): deque[(minute_ts, buy_usd, sell_usd)], maxlen=250}

Метрики считаются раз в METRICS_EVERY_SEC=10с на монету.

Вердикты:
    trend_fuel   — toxic_1h ≥ alert И vol ≥ min И знак(ret_30s) = flow_dir
                   И |ret_30s| ≥ toxic_trend_min_ret_bps И OBI подтверждает
    accumulation — toxic_1h ≥ alert, но цена стоит / идёт против / нет OBI
    ""           — токсичность ниже порога

Зарождение (ignition): trend_fuel-подобное, но цена ЕЩЁ не убежала,
токсичность растёт, ширина рынка подтверждает, есть контекст (ливка).

Ширина (breadth): long_pressure / short_pressure — по OBI-правилу,
trend_fuel / accumulation — по вердиктам. Разница в п.п.
"""
import math
import time
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Tuple

from collector_settings import SCHEMA_VERSION
from states import CoinState, coin_state as _coin_state

BUCKET_MAXLEN = 250
METRICS_EVERY_SEC = 10.0


def _minute(ts: float) -> int:
    return int(ts // 60) * 60




def ignition_check(toxic_1h: float, dir_: int, vol_1h: float,
                   ret_30s: Optional[float],
                   toxic_slope: float, breadth_hot: int,
                   context_type: Optional[str],
                   obi_now: Optional[float], obi_vel: Optional[float],
                   S) -> bool:
    """
    Зарождение тренда: лента + стакан + ширина + контекст сходятся,
    но цена ещё не пошла (|ret_30s| < ignition_max_ret_bps).
    """
    if not getattr(S, "ignition_enabled", True):
        return False
    if toxic_1h < getattr(S, "toxic_alert", 0.70):
        return False
    min_vol = getattr(S, "toxic_min_vol_usd", 0.0)
    if min_vol > 0.0 and vol_1h < min_vol:
        return False
    if dir_ == 0:
        return False
    if ret_30s is None:
        return False
    if abs(ret_30s) >= getattr(S, "ignition_max_ret_bps", 6.0):
        return False
    if dir_ > 0 and ret_30s < 0:
        return False
    if dir_ < 0 and ret_30s > 0:
        return False

    obi_vel_min = getattr(S, "obi_vel_min", 0.05)
    obi_min = getattr(S, "obi_min", 0.15)
    if obi_now is None or obi_vel is None:
        return False
    if dir_ > 0:
        if not (obi_vel > obi_vel_min and obi_now > obi_min):
            return False
    else:
        if not (obi_vel < -obi_vel_min and obi_now < -obi_min):
            return False

    if toxic_slope < getattr(S, "toxic_slope_min", 0.05):
        return False
    if breadth_hot < getattr(S, "breadth_hot_min", 5):
        return False
    if not context_type:
        return False
    return True


class ToxicScanner:
    def __init__(self, settings):
        self.S = settings
        self._buckets: Dict[Tuple[str, str], deque] = defaultdict(
            lambda: deque(maxlen=BUCKET_MAXLEN)
        )
        self._metrics: Dict[str, Dict[str, Any]] = {}
        self._last_metrics: Dict[Tuple[str, str], float] = {}
        self.stats = {"ticks": 0}

        # история toxic_1h для slope (30 последних recompute = ~5 минут)
        self._hist: Dict[str, deque] = defaultdict(lambda: deque(maxlen=30))
        self._pending_signals: List[Dict[str, Any]] = []
        self._pending_ignitions: List[Dict[str, Any]] = []
        self._prev_verdict: Dict[str, str] = {}
        self._prev_ignition: Dict[str, bool] = {}

    # --------------------------------------------------------------- приём
    def tick(self, ex: str, sym: str, price: float, qty: float,
             is_buyer_maker: bool, ts: float) -> None:
        """Вызывается на каждый aggTrade."""
        try:
            usd = float(price) * float(qty)
        except (TypeError, ValueError):
            return
        if not math.isfinite(usd) or usd <= 0:
            return
        buy = 0.0 if is_buyer_maker else usd
        sell = usd if is_buyer_maker else 0.0
        key = (ex.lower(), sym.upper())
        m = _minute(ts)
        b = self._buckets[key]
        if b and b[-1][0] == m:
            t, bb, ss = b[-1]
            b[-1] = (t, bb + buy, ss + sell)
        else:
            b.append((m, buy, sell))
        self.stats["ticks"] += 1

    # --------------------------------------------------------------- метрики
    def _sum_window(self, key: Tuple[str, str], now: float, window_sec: float
                    ) -> Tuple[float, float]:
        b = self._buckets.get(key)
        if not b:
            return 0.0, 0.0
        cutoff = now - window_sec
        buy = sell = 0.0
        for m, bb, ss in b:
            if m + 60 > cutoff:
                buy += bb
                sell += ss
        return buy, sell

    def _compute(self, key: Tuple[str, str], now: float) -> Optional[Dict[str, Any]]:
        buy_1h, sell_1h = self._sum_window(key, now, 3600.0)
        buy_4h, sell_4h = self._sum_window(key, now, 4 * 3600.0)
        tot_1h = buy_1h + sell_1h
        tot_4h = buy_4h + sell_4h
        if tot_1h <= 0 and tot_4h <= 0:
            return None
        toxic_1h = abs(buy_1h - sell_1h) / tot_1h if tot_1h > 0 else 0.0
        toxic_4h = abs(buy_4h - sell_4h) / tot_4h if tot_4h > 0 else 0.0
        flow_dir = 1 if buy_1h > sell_1h else (-1 if sell_1h > buy_1h else 0)
        return {
            "ts": now,
            "toxic_1h": round(toxic_1h, 4),
            "toxic_4h": round(toxic_4h, 4),
            "flow_dir": flow_dir,
            "vol_1h_usd": round(tot_1h, 2),
            "vol_4h_usd": round(tot_4h, 2),
            "buy_1h_usd": round(buy_1h, 2),
            "sell_1h_usd": round(sell_1h, 2),
        }

    def toxic_slope(self, sym: str, window_sec: float = 300.0) -> float:
        h = self._hist.get(sym.upper())
        if not h or len(h) < 2:
            return 0.0
        now = h[-1][0]
        oldest = h[0]
        for t, v in h:
            if now - t <= window_sec:
                oldest = (t, v)
                break
        return round(h[-1][1] - oldest[1], 4)

    def take_signals(self) -> List[Dict[str, Any]]:
        out, self._pending_signals = self._pending_signals, []
        return out

    def take_ignitions(self) -> List[Dict[str, Any]]:
        out, self._pending_ignitions = self._pending_ignitions, []
        return out

    def recompute(self, ret_30s_fn, context_fn=None, obi_fn=None,
                  now: Optional[float] = None, oi_fn=None) -> None:
        now = now or time.time()
        br = self.breadth()
        long_now = br.get("long_pressure", 0)
        short_now = br.get("short_pressure", 0)
        for key in list(self._buckets.keys()):
            if now - self._last_metrics.get(key, 0.0) < METRICS_EVERY_SEC:
                continue
            m = self._compute(key, now)
            if not m:
                continue
            self._last_metrics[key] = now
            sym = key[1]
            ret_30s = None
            if callable(ret_30s_fn):
                try:
                    ret_30s = ret_30s_fn(sym, now)
                except Exception:
                    pass

            obi_now, obi_vel = None, None
            if callable(obi_fn):
                try:
                    res = obi_fn(sym)
                    if res and len(res) == 2:
                        obi_now, obi_vel = res
                except Exception:
                    pass

            oi_val, oi_trend = None, None
            if callable(oi_fn):
                try:
                    res = oi_fn(sym, now)
                    if res and len(res) == 2:
                        oi_val, oi_trend = res
                except Exception:
                    pass

            ret_bps = None if ret_30s is None else round(float(ret_30s), 2)
            
            min_vol = getattr(self.S, "toxic_min_vol_usd", 0.0)
            vol_ok = (min_vol <= 0.0) or (m["vol_1h_usd"] >= min_vol)
            obi_vel_min = getattr(self.S, "obi_vel_min", 0.05)
            obi_min_val = getattr(self.S, "obi_min", 0.15)
            obi_ok = False
            if obi_now is not None and obi_vel is not None:
                if m["flow_dir"] > 0:
                    obi_ok = (obi_vel > obi_vel_min) and (obi_now > obi_min_val)
                elif m["flow_dir"] < 0:
                    obi_ok = (obi_vel < -obi_vel_min) and (obi_now < -obi_min_val)
            state_p = {
                "alert": getattr(self.S, "toxic_alert", 0.70),
                "min_ret": getattr(self.S, "toxic_trend_min_ret_bps", 4.0),
                "oi_drop": getattr(self.S, "oi_squeeze_max_drop_pct", -1.0),
            }
            c_state = _coin_state(
                m["toxic_1h"], m["flow_dir"], vol_ok, ret_bps, obi_ok, oi_trend, state_p
            ).value

            m["coin_state"] = c_state
            m["verdict"] = c_state  # map coin_state to verdict for compatibility
            m["ret_30s_bps"] = ret_bps
            m["obi_now"] = obi_now
            m["obi_vel"] = obi_vel
            m["exchange"] = key[0]
            m["oi"] = oi_val
            m["oi_trend_15m"] = oi_trend

            self._hist[sym].append((now, m["toxic_1h"]))

            prev_v = self._prev_verdict.get(sym, "")
            new_v = m["verdict"]
            
            if not hasattr(self, "_state_since"):
                self._state_since = {}
            if new_v != prev_v or sym not in self._state_since:
                self._state_since[sym] = now
            
            m["state_age_sec"] = now - self._state_since[sym]

            if new_v != prev_v:
                self._pending_signals.append({
                    "ts": now, "symbol": sym,
                    "verdict_new": new_v, "verdict_prev": prev_v,
                    "toxic_1h": m["toxic_1h"], "toxic_4h": m["toxic_4h"],
                    "flow_dir": m["flow_dir"], "vol_1h_usd": m["vol_1h_usd"],
                    "ret_30s_bps": m["ret_30s_bps"],
                    "obi_now": obi_now, "obi_vel": obi_vel,
                    "oi": oi_val, "oi_trend_15m": oi_trend,
                    "_horizons": (1.0, 5.0, 30.0, 60.0, 300.0),
                })
                self._prev_verdict[sym] = new_v

            if getattr(self.S, "ignition_enabled", True):
                slope = self.toxic_slope(sym)
                breadth_hot = long_now if m["flow_dir"] > 0 else short_now
                ctx_type, ctx_val = None, None
                if context_fn is not None and breadth_hot >= getattr(self.S, "breadth_hot_min", 5):
                    try:
                        res = context_fn(sym, m["flow_dir"])
                        if isinstance(res, (tuple, list)):
                            ctx_type = res[0]
                            ctx_val = res[1] if len(res) > 1 else None
                    except Exception:
                        ctx_type, ctx_val = None, None
                ign = ignition_check(
                    m["toxic_1h"], m["flow_dir"], m["vol_1h_usd"], ret_bps,
                    slope, breadth_hot, ctx_type, obi_now, obi_vel, self.S
                )
                prev_ign = self._prev_ignition.get(sym, False)
                if ign and not prev_ign:
                    self._pending_ignitions.append({
                        "ts": now, "symbol": sym,
                        "verdict": m["verdict"],
                        "toxic_1h": m["toxic_1h"], "toxic_4h": m["toxic_4h"],
                        "flow_dir": m["flow_dir"], "vol_1h_usd": m["vol_1h_usd"],
                        "toxic_slope": slope, "breadth_hot": breadth_hot,
                        "context_type": ctx_type, "context_value": ctx_val,
                        "ret_30s_bps": m["ret_30s_bps"],
                        "obi_now": obi_now, "obi_vel": obi_vel,
                        "_horizons": (30.0, 60.0, 120.0, 300.0, 900.0, 1800.0),
                    })
                self._prev_ignition[sym] = ign

            self._metrics[sym] = m

    def top_by_toxic(self, n: int = 20) -> List[Dict[str, Any]]:
        items = list(self._metrics.items())
        items.sort(key=lambda kv: kv[1].get("toxic_1h", 0.0), reverse=True)
        out = []
        for sym, m in items[:n]:
            out.append({
                "symbol": sym,
                "dir": m.get("flow_dir", 0),
                "toxic_1h": m.get("toxic_1h", 0.0),
                "toxic_4h": m.get("toxic_4h", 0.0),
                "vol_1h_usd": m.get("vol_1h_usd", 0.0),
                "ret_30s_bps": m.get("ret_30s_bps"),
                "obi_now": m.get("obi_now"),
                "obi_vel": m.get("obi_vel"),
                "verdict": m.get("verdict", ""),
                "coin_state": m.get("coin_state", "flat"),
                "state_age_sec": m.get("state_age_sec", 0.0),
                "oi": m.get("oi"),
                "oi_trend_15m": m.get("oi_trend_15m"),
            })
        return out

    def breadth(self) -> Dict[str, Any]:
        """
        long_pressure / short_pressure — по OBI-правилу (ТЗ п.2).
        market_state — из states.py (CHOP, LONG_TREND, SHORT_TREND, SQUEEZE_RISK, MIXED)
        diff_pp — разница (long - short) / total * 100.
        """
        from states import market_state as _market_state, CoinState
        total = len(self._metrics)
        obi_vel_min = getattr(self.S, "obi_vel_min", 0.05)
        obi_min = getattr(self.S, "obi_min", 0.15)
        long_pressure = short_pressure = 0
        
        state_counts = {s.value: 0 for s in CoinState}
        coin_states = []
        
        for m in self._metrics.values():
            obi_now = m.get("obi_now")
            obi_vel = m.get("obi_vel")
            if obi_now is not None and obi_vel is not None:
                if obi_vel > obi_vel_min and obi_now > obi_min:
                    long_pressure += 1
                elif obi_vel < -obi_vel_min and obi_now < -obi_min:
                    short_pressure += 1
            
            c_val = m.get("coin_state", "flat")
            try:
                c = CoinState(c_val)
                coin_states.append(c)
                state_counts[c.value] += 1
            except ValueError:
                pass
                
        diff_pp = round((long_pressure - short_pressure) / total * 100.0, 1) if total > 0 else 0.0
        
        m_state_p = {
            "trend_frac": getattr(self.S, "trend_frac", 0.4),
            "squeeze_frac": getattr(self.S, "squeeze_frac", 0.3),
        }
        m_state = _market_state(coin_states, m_state_p).value
        
        return {
            "total": total,
            "long_pressure": long_pressure,
            "short_pressure": short_pressure,
            "diff_pp": diff_pp,
            "state_counts": state_counts,
            "market_state": m_state,
            "alert": getattr(self.S, "toxic_alert", 0.70),
            "move_pp": getattr(self.S, "breadth_move_pp", 40.0),
        }

    def metrics(self, sym: str) -> Optional[Dict[str, Any]]:
        return self._metrics.get(sym.upper())

    def health(self) -> Dict[str, Any]:
        return {"pairs": len(self._metrics), "ticks": self.stats["ticks"]}
