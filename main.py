"""
main.py — запуск радара: движок + стримы Binance + дашборд.

    python main.py                             весь universe Binance USDT-M
    SYMBOLS=BTCUSDT,ETHUSDT python main.py     подмножество
"""
import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.request

import config as C
from collector_settings import load_settings
from dashboard import Dashboard
from engine import OBIEngine
from oi_tracker import OITracker
from stream import StreamManager
import healthcheck

logging.basicConfig(level=getattr(logging, C.LOG_LEVEL.upper(), logging.INFO),
                    format="%(asctime)s | %(levelname)-7s | %(name)-12s | %(message)s")
log = logging.getLogger("main")


def _uncaught_handler(exctype, value, tb):
    if issubclass(exctype, KeyboardInterrupt):
        sys.__excepthook__(exctype, value, tb)
        return
    log.critical("Uncaught top-level exception:", exc_info=(exctype, value, tb))

sys.excepthook = _uncaught_handler

STOP = False


def _handle_stop(*_a):
    global STOP
    STOP = True


def fetch_binance_universe() -> list:
    """Все USDT-M перпы Binance со статусом TRADING."""
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode())
    out = []
    for s in d.get("symbols", []):
        if (s.get("status") == "TRADING"
                and s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"):
            out.append(s["symbol"].upper())
    return out


def fetch_binance_volumes() -> dict:
    """24h quoteVolume (USD) для всех фьючерсных тикеров."""
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode())
    return {item["symbol"].upper(): float(item.get("quoteVolume", 0.0)) for item in data}


def main():
    S = load_settings()
    syms_env = [s.strip().upper() for s in os.getenv("SYMBOLS", "").split(",") if s.strip()]
    if syms_env:
        symbols = syms_env
        log.info(f"[main] SYMBOLS из окружения: {len(symbols)}")
    else:
        try:
            symbols = fetch_binance_universe()
            log.info(f"[main] universe с exchangeInfo: {len(symbols)} USDT-M перпов")
        except Exception as e:
            log.error(f"[main] не удалось получить universe: {e}")
            symbols = []

        # Фильтр по суточному обороту
        min_vol = getattr(S, "min_vol_24h_usd", 3_000_000.0)
        if min_vol > 0 and symbols:
            try:
                vols = fetch_binance_volumes()
                before = len(symbols)
                symbols = [s for s in symbols if vols.get(s, 0.0) >= min_vol]
                log.info(
                    f"[main] фильтр объёма ≥ ${min_vol/1e6:.0f}M/день: "
                    f"{before} → {len(symbols)} символов"
                )
            except Exception as e:
                log.warning(f"[main] не удалось получить объёмы, фильтр пропущен: {e}")

    if not symbols:
        log.error("[main] нет символов для подписки — выходим")
        return

    oi_tracker = OITracker()
    eng = OBIEngine(lead_exchange=C.LEAD_EXCHANGE, symbols=symbols, settings=S, oi_tracker=oi_tracker)

    dash = Dashboard(settings=S)
    if S.dash_enabled:
        dash.start()
        log.info(f"[main] dashboard http://{S.dash_host}:{S.dash_port}")

    sm = StreamManager(
        symbols=symbols,
        on_depth=eng.on_depth_update,
        on_agg_trade=eng.on_agg_trade,
        on_liquidation=eng.on_liquidation,
    )

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_stop)
        except (OSError, ValueError):
            pass

    async def _run():
        await sm.start_all()
        last_hc = time.time() - 20.0
        streams_st = None
        while not STOP:
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break

            now = time.time()
            if now - last_hc >= 30.0:
                last_hc = now
                try:
                    streams_st = healthcheck.check_connections(sm)
                except Exception as e:
                    log.error(f"[main] healthcheck failed: {e}", exc_info=True)

            try:
                await asyncio.to_thread(oi_tracker.poll, symbols)
            except Exception as e:
                log.error(f"[main] oi_tracker poll failed: {e}", exc_info=True)

            try:
                eng.tick()   # периодический пересчёт метрик и трекинг forward-return
            except Exception as e:
                log.error(f"[main] tick failed: {e}", exc_info=True)
            try:
                dash.publish(eng, streams=streams_st)
            except Exception as e:
                log.error(f"[main] dash failed: {e}", exc_info=True)

        try:
            await sm.stop_all()
        except Exception as e:
            log.error(f"[main] stop_all failed: {e}", exc_info=True)

    log.info("[main] collecting... Ctrl+C to stop")
    while not STOP:
        try:
            asyncio.run(_run())
            if not STOP:
                log.warning("[main] _run() вышел без STOP — рестарт через 5с")
                time.sleep(5.0)
        except KeyboardInterrupt:
            log.info("[main] KeyboardInterrupt")
            break
        except Exception as e:
            log.exception(f"[main] event loop error: {e}; рестарт через 5с")
            time.sleep(5.0)
        except BaseException as e:
            log.critical(f"[main] fatal: {type(e).__name__}: {e}", exc_info=True)
            break

    log.info("[main] stopped")


if __name__ == "__main__":
    threading.current_thread().name = "main"
    main()
