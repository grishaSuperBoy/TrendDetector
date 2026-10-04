"""healthcheck.py — проверка живости WebSocket-стримов Binance."""
import logging
import time
from typing import Any, Dict

log = logging.getLogger("healthcheck")


def check_connections(stream_manager=None, max_silence_sec: float = 90.0) -> Dict[str, Any]:
    """
    Возвращает {"status": "PASS"|"WARN"|"FAIL", "streams": [...], "venues": {...}}
    """
    result: Dict[str, Any] = {"status": "PASS", "streams": [], "venues": {}}
    if stream_manager is None:
        return result

    try:
        status_dict = stream_manager.get_status()
    except Exception:
        status_dict = {}

    expected = list(status_dict.keys()) or list(getattr(stream_manager, "streams", {}).keys())

    failed = warn = 0
    for name in expected:
        info = status_dict.get(name)
        if info is None:
            result["streams"].append({"name": name, "state": "missing", "silence_sec": None})
            failed += 1
            continue
        silence = info.get("seconds_since_last_msg")
        connected = info.get("connected", False)
        if silence is None:
            result["streams"].append({"name": name, "state": "WARN", "silence_sec": None})
            warn += 1
        elif not connected or silence > max_silence_sec:
            result["streams"].append({"name": name, "state": "FAIL", "silence_sec": silence})
            failed += 1
        else:
            result["streams"].append({"name": name, "state": "PASS", "silence_sec": silence})

    total = len(expected)
    if failed > 0 or warn > 0:
        result["status"] = "WARN"
    if total > 0 and (failed / total) > 0.5:
        result["status"] = "FAIL"

    result["venues"] = {"binance": {"total": total, "failed": failed}}
    log.info(f"[healthcheck] status={result['status']} failed={failed}/{total}")
    return result
