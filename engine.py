"""
engine.py — ядро радара. Только Binance, только три слоя:
  1. Токсичность ленты (VPIN-лайт) → ToxicScanner.
  2. OBI и его скорость (намерение стакана).
  3. Ширина рынка (по OBI-правилу) → ToxicScanner.breadth().

Дислокаций нет. Paper-трейдов нет. Ничего не пишется на диск.

Интерфейс (имена не менять — на них завязан main):
  on_depth_update(snap)
  on_agg_trade(symbol, price, qty, is_buyer_maker, ts=0.0, exchange=None)
  on_liquidation(event)
  extract_and_reset_unflushed() -> {"toxic_signals": [...], "ignition_events": [...]}
  get_snapshot(), health()

Каждый публичный метод обёрнут: исключение не уходит наружу,
считается в health()["errors"].
"""
import functools
import logging
import math
import time
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Tuple

from collector_settings import SCHEMA_VERSION, Settings, load_settings, verify_integrity
from toxic_scanner import ToxicScanner

log = logging.getLogger("engine")

RING_DT = 0.5
RING_LEN = 120          # 60с при шаге 0.5с

SIG_TIMEOUT_SEC = 330.0           # 5 мин (макс горизонт fwd_300s) + 30с запас
IGN_TIMEOUT_SEC = 1860.0          # 30 мин (макс горизонт fwd_1800s) + 60с запас
IGN_RECENT_MAX_AGE_SEC = 1800.0   # 30 мин отображения в активных ignition


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        try:
            return fn(self, *a, **kw)
        except Exception as e:  # noqa: BLE001
            self._on_error(fn.__name__, e)
            return None
    return wrapper


class OBIEngine:
    def __init__(
        self,
        lead_exchange: str = "binance",
        symbols: Optional[List[str]] = None,
        settings: Optional[Settings] = None,
        oi_tracker: Optional[Any] = None,
        **_legacy,
    ):
        self._errors: Dict[str, int] = {}
        self.c: Dict[str, int] = defaultdict(int)
        self.S: Settings = settings or load_settings()
        self._oi_tracker = oi_tracker
        S = self.S

        ok, details = verify_integrity()
        self.integrity = "OK" if ok else ("NO_MANIFEST" if ok is None else "MODIFIED")
        if ok is False:
            log.error("[engine] !!! КОД ИЗМЕНЁН: " + "; ".join(details))
            if S.strict_integrity:
                raise RuntimeError("integrity check failed: " + "; ".join(details))

        self.lead_exchange = lead_exchange.lower()
        self.symbols = [s.upper() for s in (symbols or [])]
        self._t_start = time.time()

        # состояние рынка
        self._books: Dict[str, Any] = {}                 # symbol -> последний snap (Binance)
        self._recv: Dict[str, float] = {}                # symbol -> ts получения
        self._mid_ring: Dict[str, deque] = {}            # symbol -> deque[(ts, mid)]
        self._obi_ring: Dict[str, deque] = {}            # symbol -> deque[(ts, obi)]
        self._norm: Dict[str, Dict[str, float]] = {}     # symbol -> {sp, dp, t}

        # ликвидации (для контекста ignition)
        self._liqs: Dict[str, deque] = defaultdict(lambda: deque(maxlen=500))

        # трекинг forward-return для toxic_signals и ignition_events
        self._sig_track: List[Dict[str, Any]] = []
        self._ign_track: List[Dict[str, Any]] = []
        self._sig_by_sym: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._ign_by_sym: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._recent_ignitions: List[Dict[str, Any]] = []

        cap = S.unflushed_cap
        self._unf: Dict[str, deque] = {
            t: deque(maxlen=cap) for t in ("toxic_signals", "ignition_events")
        }
        self._toxic = ToxicScanner(S)

    # ------------------------------------------------------------- служебное
    def _on_error(self, name: str, e: Exception):
        n = self._errors[name] = self._errors.get(name, 0) + 1
        if n <= 5 or n % 1000 == 0:
            log.error(f"[engine] {name} error #{n}: {type(e).__name__}: {e}", exc_info=True)

    def _emit(self, table: str, row: Dict[str, Any]):
        d = self._unf[table]
        if len(d) == d.maxlen:
            self.c[f"overflow_{table}"] += 1
        d.append(row)
        self.c[f"emitted_{table}"] += 1

    # ============================================================ ордербук
    @_safe
    def on_depth_update(self, snap):
        now = time.time()
        mid = snap.mid_price
        if not (mid > 0) or not math.isfinite(mid):
            self.c["bad_snaps"] += 1
            return
        sym = snap.symbol.upper()
        self._books[sym] = snap
        self._recv[sym] = now
        self.c["depth_updates"] += 1

        r = self._mid_ring.get(sym)
        if r is None:
            r = self._mid_ring[sym] = deque(maxlen=RING_LEN)
        if not r or now - r[-1][0] >= RING_DT:
            r.append((now, mid))

        ro = self._obi_ring.get(sym)
        if ro is None:
            ro = self._obi_ring[sym] = deque(maxlen=RING_LEN)
        obi = float(getattr(snap, "obi", 0.0) or 0.0)
        if not ro or now - ro[-1][0] >= RING_DT:
            ro.append((now, obi))

        self._norm_update(sym, snap, now)
        self._forward_tick(sym, now)

    def _norm_update(self, sym: str, snap, now: float):
        depth = snap.depth_bid_usd + snap.depth_ask_usd
        n = self._norm.get(sym)
        if n is None:
            self._norm[sym] = {"sp": snap.spread_bps, "dp": depth, "t": now}
        elif now - n["t"] >= 1.0:
            a = 1.0 - math.exp(-(now - n["t"]) / 600.0)
            n["sp"] += a * (snap.spread_bps - n["sp"])
            n["dp"] += a * (depth - n["dp"])
            n["t"] = now

    # ============================================================ лента
    @_safe
    def on_agg_trade(self, symbol: str, price: float, qty: float,
                     is_buyer_maker: bool, ts: float = 0.0, exchange: Optional[str] = None):
        now = time.time()
        self._toxic.tick(self.lead_exchange, symbol, price, qty,
                         is_buyer_maker, ts if ts > 0 else now)

    # ============================================================ ликвидации
    @_safe
    def on_liquidation(self, event):
        now = time.time()
        sym = event.symbol.upper()
        side = str(getattr(event, "side", "")).upper()
        usd = float(getattr(event, "qty_usd", 0.0))
        self._liqs[sym].append((now, side, usd))
        self.c["liquidations"] += 1

    # ============================================================ OBI velocity
    def _obi_pair(self, sym: str) -> Tuple[Optional[float], Optional[float]]:
        """(obi_now, obi_vel) за окно S.obi_vel_window_sec."""
        sym = sym.upper()
        snap = self._books.get(sym)
        if snap is None:
            return None, None
        obi_now = float(getattr(snap, "obi", 0.0) or 0.0)
        ring = self._obi_ring.get(sym)
        if not ring:
            return obi_now, 0.0
        now = time.time()
        w = getattr(self.S, "obi_vel_window_sec", 30.0)
        target = now - w
        obi_past = None
        for t, v in reversed(ring):
            if t <= target:
                obi_past = v
                break
        if obi_past is None:
            t0, v0 = ring[0]
            if now - t0 >= 0.5 * w:
                obi_past = v0
            else:
                return round(obi_now, 4), 0.0
        return round(obi_now, 4), round(obi_now - obi_past, 4)

    # ============================================================ ret_30s
    def _ret_30s(self, sym: str, now: float) -> Optional[float]:
        sym = sym.upper()
        snap = self._books.get(sym)
        if snap is None or snap.mid_price <= 0:
            return None
        ring = self._mid_ring.get(sym)
        if not ring:
            return None
        target = now - 30.0
        ref = None
        for t, m in reversed(ring):
            if t <= target:
                ref = m
                break
        if ref is None:
            t0, m0 = ring[0]
            if now - t0 >= 15.0:
                ref = m0
            else:
                return None
        return round((snap.mid_price - ref) / ref * 1e4, 2)

    # ============================================================ контекст
    def _ignition_context(self, sym: str, flow_dir: int) -> Tuple[Optional[str], Optional[str]]:
        """Возвращает ('liq', '$120k') или (None, None).
        Порог и окно — из Settings, никаких хардкодов."""
        S = self.S
        min_usd = getattr(S, "ignition_context_liq_usd", 0.0)
        window = getattr(S, "ignition_context_window_sec", 5.0)
        if min_usd <= 0.0:
            return "liq", "off"      # отключено — контекст считается пройденным
        now = time.time()
        target_side = "BUY" if flow_dir > 0 else "SELL"
        sym = sym.upper()
        liq_sum = 0.0
        for t, side, usd in reversed(self._liqs.get(sym, ())):
            if now - t > window:
                break
            if side == target_side:
                liq_sum += usd
        if liq_sum >= min_usd:
            if liq_sum >= 1000:
                val = f"${int(liq_sum / 1000)}k"
            else:
                val = f"${int(liq_sum)}"
            return "liq", val
        return None, None

    # ============================================================ forward-return
    @_safe
    def _forward_tick(self, sym: str, now: float):
        """Добивает forward-return для toxic_signals и ignition_events по обновлениям стакана."""
        sig_list = self._sig_by_sym.get(sym)
        ign_list = self._ign_by_sym.get(sym)
        if not sig_list and not ign_list:
            return
        snap = self._books.get(sym)
        cur_mid = snap.mid_price if snap and snap.mid_price > 0 else None
        ring = self._mid_ring.get(sym)

        def _price_at(ts_target):
            if ring is None:
                return cur_mid
            for t, m in ring:
                if t >= ts_target:
                    return m
            return cur_mid

        # toxic_signals: горизонты 1, 5, 30, 60, 300
        sig_horizons = (1.0, 5.0, 30.0, 60.0, 300.0)
        if sig_list:
            for sig in list(sig_list):
                dt = now - sig["ts"]
                mi = sig["_mi"]
                while mi < len(sig_horizons) and dt >= sig_horizons[mi]:
                    h = sig_horizons[mi]
                    ref_mid = _price_at(sig["ts"] + h)
                    if ref_mid and sig.get("mid_lead") and sig["mid_lead"] > 0:
                        sig[f"fwd_{int(h)}s_bps"] = round(
                            (ref_mid - sig["mid_lead"]) / sig["mid_lead"] * 1e4, 2)
                    mi += 1
                    sig["_mi"] = mi
                if sig["_mi"] >= len(sig_horizons):
                    sig_list.remove(sig)
                    if sig in self._sig_track:
                        self._sig_track.remove(sig)
                    row = {k: v for k, v in sig.items() if not k.startswith("_")}
                    self._emit("toxic_signals", row)

        # ignition_events: горизонты 30, 60, 120, 300, 900, 1800
        ign_horizons = (30.0, 60.0, 120.0, 300.0, 900.0, 1800.0)
        if ign_list:
            for ign in list(ign_list):
                dt = now - ign["ts"]
                mi = ign["_mi"]
                while mi < len(ign_horizons) and dt >= ign_horizons[mi]:
                    h = ign_horizons[mi]
                    ref_mid = _price_at(ign["ts"] + h)
                    if ref_mid and ign.get("mid_lead") and ign["mid_lead"] > 0:
                        ign[f"fwd_{int(h)}s_bps"] = round(
                            (ref_mid - ign["mid_lead"]) / ign["mid_lead"] * 1e4, 2)
                    mi += 1
                    ign["_mi"] = mi
                if ign["_mi"] >= len(ign_horizons):
                    ign_list.remove(ign)
                    if ign in self._ign_track:
                        self._ign_track.remove(ign)
                    row = {k: v for k, v in ign.items() if not k.startswith("_")}
                    self._emit("ignition_events", row)

    # ============================================================ sweep
    def _sweep(self, now: float):
        # зависшие сигналы (> SIG_TIMEOUT_SEC) — принудительно выгружаем
        for sig in list(self._sig_track):
            if now - sig["ts"] > SIG_TIMEOUT_SEC:
                self._sig_track.remove(sig)
                sym_list = self._sig_by_sym.get(sig["symbol"])
                if sym_list and sig in sym_list:
                    sym_list.remove(sig)
                row = {k: v for k, v in sig.items() if not k.startswith("_")}
                self._emit("toxic_signals", row)
        # зависшие ignition (> IGN_TIMEOUT_SEC)
        for ign in list(self._ign_track):
            if now - ign["ts"] > IGN_TIMEOUT_SEC:
                self._ign_track.remove(ign)
                sym_list = self._ign_by_sym.get(ign["symbol"])
                if sym_list and ign in sym_list:
                    sym_list.remove(ign)
                row = {k: v for k, v in ign.items() if not k.startswith("_")}
                self._emit("ignition_events", row)
        # чистим старые ignition из recent
        self._recent_ignitions = [e for e in self._recent_ignitions if now - e["ts"] < IGN_RECENT_MAX_AGE_SEC]

    def _oi_trend(self, sym: str, now: Optional[float] = None) -> Tuple[Optional[float], Optional[float]]:
        if self._oi_tracker is not None:
            try:
                return self._oi_tracker.trend_15m(sym, now)
            except Exception:
                pass
        return None, None

    # ============================================================ периодический такт
    @_safe
    def tick(self) -> None:
        """Периодическая работа без выгрузки на диск: sweep, recompute, трекинг."""
        now = time.time()
        self._sweep(now)
        self._toxic.recompute(self._ret_30s, self._ignition_context, self._obi_pair, now, self._oi_trend)

        for sig in self._toxic.take_signals():
            sym = sig["symbol"]
            snap = self._books.get(sym)
            mid = snap.mid_price if snap and snap.mid_price > 0 else None
            sig["mid_lead"] = mid
            sig["spread_bps"] = round(snap.spread_bps, 2) if snap else None
            sig["obi"] = round(getattr(snap, "obi", 0.0), 3) if snap else None
            sig["_mi"] = 0
            sig["v"] = SCHEMA_VERSION
            for h in (1.0, 5.0, 30.0, 60.0, 300.0):
                sig[f"fwd_{int(h)}s_bps"] = None
            if mid:
                self._sig_track.append(sig)
                self._sig_by_sym[sym].append(sig)

        for ign in self._toxic.take_ignitions():
            sym = ign["symbol"]
            snap = self._books.get(sym)
            mid = snap.mid_price if snap and snap.mid_price > 0 else None
            ign["mid_lead"] = mid
            ign["spread_bps"] = round(snap.spread_bps, 2) if snap else None
            ign["verdict_at_ts"] = ign.pop("verdict", "")
            ign["_mi"] = 0
            ign["v"] = SCHEMA_VERSION
            for h in (30.0, 60.0, 120.0, 300.0, 900.0, 1800.0):
                ign[f"fwd_{int(h)}s_bps"] = None
            if mid:
                self._ign_track.append(ign)
                self._ign_by_sym[sym].append(ign)
                self._recent_ignitions.append(dict(ign))
                if len(self._recent_ignitions) > 100:
                    self._recent_ignitions = self._recent_ignitions[-100:]

    # ============================================================ выгрузка
    @_safe
    def extract_and_reset_unflushed(self) -> Dict[str, List[Dict[str, Any]]]:
        self.tick()
        out: Dict[str, List[Dict[str, Any]]] = {}
        for table, d in self._unf.items():
            rows = []
            try:
                while True:
                    rows.append(d.popleft())
            except IndexError:
                pass
            out[table] = rows
        return out

    # ============================================================ снапшот
    def health(self) -> Dict[str, Any]:
        return {
            "uptime_sec": round(time.time() - self._t_start, 1),
            "integrity": self.integrity,
            "errors": dict(self._errors),
            "counters": dict(self.c),
            "sizes": {
                "books": len(self._books),
                "toxic_pairs": self._toxic.health()["pairs"],
                "sig_track": len(self._sig_track),
                "ign_track": len(self._ign_track),
                "unflushed": {t: len(d) for t, d in self._unf.items()},
            },
        }

    def get_snapshot(self) -> Dict[str, Any]:
        now = time.time()
        active_ignitions = []
        for e in self._recent_ignitions:
            if now - e["ts"] >= 1800.0:
                continue
            snap = self._books.get(e["symbol"])
            cur_mid = snap.mid_price if snap and snap.mid_price > 0 else None
            mid0 = e.get("mid_lead")
            ret_bps = (round((cur_mid - mid0) / mid0 * 1e4, 1)
                       if cur_mid and mid0 else 0.0)
            ctx_t = e.get("context_type")
            ctx_v = e.get("context_value")
            ctx_disp = f"{ctx_t}: {ctx_v}" if (ctx_t and ctx_v) else (ctx_t or "")
            active_ignitions.append({
                "symbol": e["symbol"],
                "dir": e["flow_dir"],
                "toxic_1h": e["toxic_1h"],
                "slope": e["toxic_slope"],
                "breadth": e["breadth_hot"],
                "context": ctx_disp,
                "age_sec": round(now - e["ts"], 1),
                "ret_bps": ret_bps,
            })
        return {
            "health": self.health(),
            "active_ignitions": active_ignitions,
            "toxic_top": self._toxic.top_by_toxic(20),
            "toxic_breadth": self._toxic.breadth(),
            "lead_exchange": self.lead_exchange,
        }
