# oi_tracker.py — OI-тренд для фильтра сквизов.
# REST-поллинг Binance в фоновом потоке с Keep-Alive, тренд в % за 15 мин.
import time
import http.client
import json
import threading
import logging
from collections import defaultdict, deque
from typing import Dict, Tuple

log = logging.getLogger("oi_tracker")

POLL_SEC = 30.0          # период опроса всего пула монет
TREND_WINDOW_SEC = 900.0 # окно тренда 15 мин
HIST_MAXLEN = 64         # ~32 мин истории при опросе 30с

class OITracker:
    def __init__(self):
        self._hist: Dict[Tuple[str, str], deque] = defaultdict(
            lambda: deque(maxlen=HIST_MAXLEN))
        
        self._symbols = []
        self._thread = None
        self._stop_event = threading.Event()

    def _fetch(self, sym: str):
        # Оставлено только для совместимости с моками в selfcheck.py
        import urllib.request
        req = urllib.request.Request(
            f"https://fapi.binance.com/fapi/v1/openInterest?symbol={sym}",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return float(json.loads(r.read().decode())["openInterest"])

    def poll(self, symbols, now=None):
        """Ручной синхронный опрос (для тестов selfcheck)."""
        now = now or time.time()
        for sym in symbols:
            key = ("binance", sym.upper())
            try:
                oi = self._fetch(sym.upper())
                self._hist[key].append((now, oi))
            except Exception:
                pass
        return {}

    def background_start(self, symbols):
        """Запуск фонового поллинга (для продакшена)."""
        self._symbols = [s.upper() for s in symbols]
        if self._thread is None:
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()
            log.info("[oi_tracker] Background worker started")

    def background_stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _worker(self):
        conn = None
        while not self._stop_event.is_set():
            if not self._symbols:
                self._stop_event.wait(1.0)
                continue

            # Размазываем опрос: пауза между монетами = POLL_SEC / количество
            # Минимум 0.05с (не более 20 реквестов в сек), максимум 1.0с
            delay = min(max(POLL_SEC / len(self._symbols), 0.05), 1.0)
            
            if conn is None:
                conn = http.client.HTTPSConnection("fapi.binance.com", timeout=5)
            
            for sym in self._symbols:
                if self._stop_event.is_set():
                    break
                
                try:
                    conn.request("GET", f"/fapi/v1/openInterest?symbol={sym}", 
                                 headers={"User-Agent": "OITracker/1.0", "Connection": "keep-alive"})
                    resp = conn.getresponse()
                    data = resp.read()
                    if resp.status == 200:
                        oi = float(json.loads(data)["openInterest"])
                        key = ("binance", sym)
                        self._hist[key].append((time.time(), oi))
                    else:
                        conn.close()
                        conn = None
                except Exception as e:
                    if conn:
                        conn.close()
                    conn = None
                
                # Спим между запросами, чтобы не спамить биржу
                self._stop_event.wait(delay)

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
