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
        """?????? ???????? ????? ????? WebSockets (??? ??????????)."""
        import asyncio
        self._symbols = [s.upper() for s in symbols]
        self._running = True
        self._task = asyncio.create_task(self._ws_loop())
        log.info("[oi_tracker] Background WS worker started")

    def background_stop(self):
        self._running = False
        if getattr(self, "_task", None):
            self._task.cancel()

    async def _ws_loop(self):
        import asyncio
        import websockets
        import json
        import time

        ws_url = "wss://fstream.binance.com/stream"
        
        # Prepare streams (Binance requires lowercase, and we must ensure USDT suffix)
        formatted_syms = [s.lower() if s.endswith("USDT") else f"{s.lower()}usdt" for s in self._symbols]
        streams = [f"{s}@openInterest" for s in formatted_syms]
        
        while self._running:
            try:
                log.info(f"[oi_tracker] Connecting to WS with {len(streams)} streams...")
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=15, max_queue=500) as ws:
                    # Binance limit is 200 streams per request, chunk them
                    for i in range(0, len(streams), 100):
                        sub = {"method": "SUBSCRIBE", "params": streams[i:i+100], "id": 2000 + i}
                        await ws.send(json.dumps(sub))
                        await asyncio.sleep(0.1)
                        
                    while self._running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        except asyncio.TimeoutError:
                            continue
                            
                        payload = json.loads(raw)
                        if "data" in payload:
                            data = payload["data"]
                            if data.get("e") == "openInterest":
                                sym = data.get("s", "").upper()
                                for orig_s in self._symbols:
                                    if sym == orig_s or sym == orig_s + "USDT":
                                        oi = float(data.get("o", 0.0))
                                        key = ("binance", orig_s)
                                        self._hist[key].append((time.time(), oi))
                                        break
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(f"[oi_tracker] WS error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5.0)

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
