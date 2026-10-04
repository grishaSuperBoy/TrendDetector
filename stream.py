"""
stream.py — WebSocket-стримы Binance Futures:
  1. Ордербук depth20@100ms (чанками по 50 монет).
  2. aggTrade (все монеты, чанками по 50 стримов).
  3. Ликвидации !forceOrder@arr (один глобальный сокет).
"""
import asyncio
from datetime import datetime, timezone, timedelta
import json
import logging
import time
from typing import Callable, Dict, List, Optional, Any

import websockets

from models import OrderBookDepth5, LiquidationEvent

log = logging.getLogger("stream")

# ---------------------------------------------------------------------------
# Daily scheduled reconnect helper
# Binance WebSocket connections are capped at 24h server-side.
# We proactively reconnect at 02:23 UTC — far from all funding settlements
# (00:00 / 08:00 / 16:00 UTC) and not aligned with any 1h or 15m candle open.
# ---------------------------------------------------------------------------
_DAILY_RECONNECT_HOUR = 2
_DAILY_RECONNECT_MINUTE = 23


def _secs_until_daily_reconnect() -> float:
    """Seconds until next 02:23 UTC (always positive, max ~24h)."""
    now = datetime.now(timezone.utc)
    target = now.replace(
        hour=_DAILY_RECONNECT_HOUR, minute=_DAILY_RECONNECT_MINUTE,
        second=0, microsecond=0,
    )
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()



class BaseStream:
    def __init__(self, name: str):
        self.name = name
        self.connected = False
        self.last_msg_ts = 0.0
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.connected = False

    async def _run_loop(self):
        raise NotImplementedError


class BinanceDepthStream(BaseStream):
    """Ордербук Binance Futures. Чанк из <=50 символов на один сокет."""

    def __init__(self, symbols: List[str], callback: Callable[[OrderBookDepth5], None],
                 chunk_id: int = 0):
        super().__init__(f"binance_{chunk_id}")
        self.symbols = [s.upper() for s in symbols]
        self.callback = callback

    async def _run_loop(self):
        if not self.symbols:
            return
        formatted = [s.lower() if s.lower().endswith("usdt") else f"{s.lower()}usdt" for s in self.symbols]
        sub_streams = [f"{s}@depth20@100ms" for s in formatted]
        url = "wss://fstream.binance.com/stream?streams=" + "/".join(sub_streams)

        backoff = 3.0
        while self._running:
            try:
                log.info(f"[{self.name}] Connecting ({len(self.symbols)} symbols)...")
                async with websockets.connect(url, ping_interval=20, ping_timeout=15,
                                              max_queue=2000) as ws:
                    self.connected = True
                    log.info(f"[{self.name}] Connected.")
                    reconnect_in = _secs_until_daily_reconnect()
                    deadline = time.monotonic() + reconnect_in
                    while self._running:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            log.info(f"[{self.name}] Scheduled daily reconnect at 02:23 UTC. Reconnecting...")
                            break
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=min(30.0, remaining))
                        except asyncio.TimeoutError:
                            continue
                        self.last_msg_ts = time.time()
                        backoff = 3.0
                        self._parse(msg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.connected = False
                log.warning(f"[{self.name}] disconnect: {e!r}; reconnect {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 20.0)

    def _parse(self, raw: str):
        try:
            payload = json.loads(raw)
            data = payload.get("data", payload)
            sym = data.get("s")
            if not sym:
                return
            raw_bids = data.get("b", [])
            raw_asks = data.get("a", [])
            if not raw_bids or not raw_asks:
                return
            bids = [(float(p), float(q)) for p, q in raw_bids[:20]]
            asks = [(float(p), float(q)) for p, q in raw_asks[:20]]
            snap = OrderBookDepth5(
                exchange="binance", symbol=sym.upper(),
                ts=time.time(), exchange_ts=int(data.get("E", 0)),
                bids=bids, asks=asks,
            )
            self.callback(snap)
        except Exception:
            pass


class BinanceAggTradeStream(BaseStream):
    """Лента сделок aggTrade. Чанк из <=50 стримов на один сокет."""

    def __init__(self, symbols: List[str],
                 callback: Callable[[str, float, float, bool, float], None],
                 chunk_id: int = 0):
        super().__init__(f"binance_agg_{chunk_id}")
        self.symbols = [s.upper() for s in symbols]
        self.callback = callback

    async def _run_loop(self):
        if not self.symbols:
            return
        formatted = [s.lower() if s.lower().endswith("usdt") else f"{s.lower()}usdt" for s in self.symbols]
        sub_streams = [f"{s}@aggTrade" for s in formatted]
        url = "wss://fstream.binance.com/stream?streams=" + "/".join(sub_streams)

        backoff = 3.0
        while self._running:
            try:
                log.info(f"[{self.name}] Connecting ({len(self.symbols)} symbols)...")
                async with websockets.connect(url, ping_interval=20, ping_timeout=15,
                                              max_queue=5000) as ws:
                    self.connected = True
                    log.info(f"[{self.name}] Connected.")
                    reconnect_in = _secs_until_daily_reconnect()
                    deadline = time.monotonic() + reconnect_in
                    while self._running:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            log.info(f"[{self.name}] Scheduled daily reconnect at 02:23 UTC. Reconnecting...")
                            break
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=min(90.0, remaining))
                        except asyncio.TimeoutError:
                            continue
                        self.last_msg_ts = time.time()
                        backoff = 3.0
                        self._parse(msg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.connected = False
                log.warning(f"[{self.name}] disconnect: {e!r}; reconnect {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 20.0)

    def _parse(self, raw: str):
        try:
            payload = json.loads(raw)
            data = payload.get("data", payload)
            sym = data.get("s", "").upper()
            if not sym:
                return
            price = float(data.get("p", 0.0))
            qty = float(data.get("q", 0.0))
            is_buyer_maker = bool(data.get("m", False))
            ts = float(data.get("T", time.time() * 1000)) / 1000.0
            self.callback(sym, price, qty, is_buyer_maker, ts)
        except Exception:
            pass


class BinanceLiquidationStream(BaseStream):
    """Глобальный !forceOrder@arr — один сокет на весь рынок."""

    def __init__(self, callback: Callable[[LiquidationEvent], None]):
        super().__init__("binance_liquidations")
        self.callback = callback

    async def _run_loop(self):
        url = "wss://fstream.binance.com/ws/!forceOrder@arr"
        while self._running:
            try:
                log.info(f"[{self.name}] Connecting...")
                async with websockets.connect(url, ping_interval=20, ping_timeout=15,
                                              max_queue=2000) as ws:
                    self.connected = True
                    log.info(f"[{self.name}] Connected.")
                    reconnect_in = _secs_until_daily_reconnect()
                    deadline = time.monotonic() + reconnect_in
                    while self._running:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            log.info(f"[{self.name}] Scheduled daily reconnect at 02:23 UTC. Reconnecting...")
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=min(30.0, remaining))
                        except asyncio.TimeoutError:
                            continue
                        self.last_msg_ts = time.time()
                        self._parse(raw)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.connected = False
                log.warning(f"[{self.name}] disconnect: {e!r}; reconnect 3s")
                await asyncio.sleep(3.0)

    def _parse(self, raw: str):
        try:
            data = json.loads(raw)
            order = data.get("o", {})
            sym = order.get("s", "").upper()
            if not sym:
                return
            side = order.get("S", "").upper()
            price = float(order.get("p", 0.0))
            qty = float(order.get("q", 0.0))
            event = LiquidationEvent(
                id=str(order.get("T", int(time.time() * 1000))),
                symbol=sym, side=side, price=price, qty=qty,
                qty_usd=price * qty, exchange="binance", ts=time.time(),
            )
            self.callback(event)
        except Exception:
            pass


class StreamManager:
    """Запускает и держит все Binance-стримы."""

    def __init__(
        self,
        symbols: List[str],
        on_depth: Callable[[OrderBookDepth5], None],
        on_agg_trade: Callable[[str, float, float, bool, float], None],
        on_liquidation: Optional[Callable[[LiquidationEvent], None]] = None,
    ):
        self.symbols = [s.upper() for s in symbols]
        self.on_depth = on_depth
        self.on_agg_trade = on_agg_trade
        self.on_liquidation = on_liquidation
        self.streams: Dict[str, BaseStream] = {}

    async def start_all(self):
        log.info(f"StreamManager: {len(self.symbols)} symbols")
        # Ордербук чанками по 200
        for i, start in enumerate(range(0, len(self.symbols), 200)):
            chunk = self.symbols[start:start + 200]
            s = BinanceDepthStream(chunk, self.on_depth, chunk_id=i)
            self.streams[s.name] = s
        # aggTrade чанками по 200
        for i, start in enumerate(range(0, len(self.symbols), 200)):
            chunk = self.symbols[start:start + 200]
            s = BinanceAggTradeStream(chunk, self.on_agg_trade, chunk_id=i)
            self.streams[s.name] = s
        # Ликвидации — один сокет
        if self.on_liquidation:
            self.streams["binance_liquidations"] = BinanceLiquidationStream(self.on_liquidation)

        for s in self.streams.values():
            await s.start()
            await asyncio.sleep(0.25)

    async def stop_all(self):
        for s in self.streams.values():
            await s.stop()

    def get_status(self) -> Dict[str, Dict[str, Any]]:
        now = time.time()
        return {
            name: {
                "connected": getattr(s, "connected", False),
                "seconds_since_last_msg": (round(now - s.last_msg_ts, 1)
                                           if getattr(s, "last_msg_ts", 0) > 0 else None),
            }
            for name, s in self.streams.items()
        }
