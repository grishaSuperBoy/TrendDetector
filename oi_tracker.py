# oi_tracker.py — OI-тренд для фильтра сквизов.
# REST-поллинг Binance (WS-стрима OI не существует), тренд в % за 15 мин.
import time
import urllib.request
import json
from collections import defaultdict, deque
from typing import Dict, Tuple

OI_URL = "https://fapi.binance.com/fapi/v1/openInterest?symbol={sym}"
POLL_SEC = 30.0          # структура: период опроса
TREND_WINDOW_SEC = 900.0 # структура: окно тренда 15 мин
HIST_MAXLEN = 64         # структура: ~32 мин истории при опросе 30с


class OITracker:
    def __init__(self):
        self._hist: Dict[Tuple[str, str], deque] = defaultdict(
            lambda: deque(maxlen=HIST_MAXLEN))
        self._last_poll: Dict[Tuple[str, str], float] = {}

    def _fetch(self, sym: str):
        req = urllib.request.Request(
            OI_URL.format(sym=sym),
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return float(json.loads(r.read().decode())["openInterest"])

    def poll(self, symbols, now=None):
        """Дёргать из главного цикла не чаще POLL_SEC. Возвращает {sym: oi}."""
        now = now or time.time()
        out = {}
        for sym in symbols:
            key = ("binance", sym.upper())
            if now - self._last_poll.get(key, 0.0) < POLL_SEC:
                continue
            self._last_poll[key] = now
            try:
                oi = self._fetch(sym.upper())
            except Exception:
                continue
            self._hist[key].append((now, oi))
            out[sym.upper()] = oi
        return out

    def trend_15m(self, sym, now=None):
        """(oi_now, trend_pct) или (None, None). Тренд vs значение 15 мин назад."""
        now = now or time.time()
        h = self._hist.get(("binance", sym.upper()))
        if not h:
            return None, None
        cur = h[-1][1]
        old = None
        for t, v in reversed(h):
            if now - t >= TREND_WINDOW_SEC:
                old = v
                break
        if old is None or old <= 0:
            return cur, None
        return cur, round((cur - old) / old * 100.0, 2)
