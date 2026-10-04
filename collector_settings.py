"""
collector_settings.py — все настраиваемые параметры радара.

Правило: менять ЗНАЧЕНИЯ можно в collector_settings.json (рядом с этим файлом).
Код менять не нужно. Некорректные значения отвергаются, остаётся дефолт.

Команды:
    python collector_settings.py --write-default   создать collector_settings.json
    python collector_settings.py --seal            обновить MANIFEST.sha256
"""
import hashlib
import json
import logging
import math
import os
import sys
from dataclasses import asdict, dataclass, fields
from typing import List, Optional, Tuple

log = logging.getLogger("settings")

SCHEMA_VERSION = "dl3"
HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "collector_settings.json")
MANIFEST_FILE = "MANIFEST.sha256"
CORE_FILES = (
    "engine.py", "collector_settings.py", "dashboard.py", "selfcheck.py",
    "models.py", "stream.py", "main.py", "config.py", "toxic_scanner.py",
    "healthcheck.py", "oi_tracker.py",
)


@dataclass(frozen=True)
class Settings:
    # ---- токсичность потока (VPIN-лайт)
    toxic_alert: float = 0.70              # порог toxic_1h для вердикта
    toxic_min_vol_usd: float = 500_000.0   # минимум объёма за 1ч
    toxic_trend_min_ret_bps: float = 4.0   # минимум движения цены за 30с для trend_fuel
    toxic_slope_min: float = 0.05          # рост toxic_1h за 5 мин для ignition

    # ---- OBI velocity
    obi_vel_min: float = 0.05              # минимальный рост OBI за окно
    obi_min: float = 0.15                  # минимум текущего OBI в сторону потока
    obi_vel_window_sec: float = 30.0       # окно скорости OBI

    # ---- ignition (зарождение тренда)
    ignition_enabled: bool = True
    ignition_max_ret_bps: float = 6.0      # больше — цена уже пошла
    breadth_hot_min: int = 5               # минимум монет в ту же сторону
    ignition_context_liq_usd: float = 50_000.0   # минимум ликвидаций за окно (0 = отключено)
    ignition_context_window_sec: float = 5.0     # окно для ликвидаций

    # ---- ширина рынка
    breadth_move_pp: float = 40.0          # |long - short| / total * 100 = «рынок поехал»

    # ---- буферы в RAM
    flow_maxlen: int = 1500                # принтов на (биржа, монета)
    unflushed_cap: int = 20000             # строк на таблицу

    # ---- дашборд (локальный, ничего не пишет)
    dash_enabled: bool = True
    dash_host: str = "127.0.0.1"
    dash_port: int = 8765

    # ---- защита кода
    strict_integrity: bool = False

    # ---- фильтр universe
    min_vol_24h_usd: float = 3_000_000.0   # отсечка по суточному обороту ($). 0 = без фильтра

    # ---- фильтр сквизов по OI
    oi_squeeze_max_drop_pct: float = -1.0  # порог падения OI за 15 мин для даунгрейда trend_fuel -> accumulation


_RANGES = {
    "toxic_alert": (0.5, 0.95),
    "toxic_min_vol_usd": (0.0, 100_000_000.0),
    "toxic_trend_min_ret_bps": (0.5, 100.0),
    "toxic_slope_min": (0.0, 1.0),
    "obi_vel_min": (0.001, 1.0),
    "obi_min": (0.01, 0.9),
    "obi_vel_window_sec": (5.0, 300.0),
    "ignition_max_ret_bps": (0.5, 100.0),
    "breadth_hot_min": (1, 1000),
    "ignition_context_liq_usd": (0.0, 100_000_000.0),
    "ignition_context_window_sec": (1.0, 60.0),
    "breadth_move_pp": (5.0, 100.0),
    "flow_maxlen": (100, 20000),
    "unflushed_cap": (1000, 500000),
    "dash_port": (1024, 65535),
    "min_vol_24h_usd": (0.0, 10_000_000_000.0),
    "oi_squeeze_max_drop_pct": (-10.0, 0.0),
}


def load_settings(path: Optional[str] = None) -> Settings:
    path = path or SETTINGS_FILE
    vals = {}
    raw = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                log.error("[settings] корень JSON должен быть объектом, игнорирую файл")
                raw = {}
        except Exception as e:  # noqa: BLE001
            log.error(f"[settings] не удалось прочитать {path}: {e}; беру значения по умолчанию")
            raw = {}
    known = {f.name: f for f in fields(Settings)}
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        f = known.get(k)
        if f is None:
            log.warning(f"[settings] неизвестный параметр '{k}' — игнорирую")
            continue
        default = f.default
        try:
            if isinstance(default, bool):
                if not isinstance(v, bool):
                    raise ValueError("ожидался true/false")
                cv = v
            elif isinstance(default, int):
                if isinstance(v, bool):
                    raise ValueError("ожидалось число")
                cv = int(v)
            elif isinstance(default, float):
                if isinstance(v, bool):
                    raise ValueError("ожидалось число")
                cv = float(v)
            else:
                cv = str(v)
        except (TypeError, ValueError) as e:
            log.warning(f"[settings] '{k}'={v!r} отвергнут: {e}")
            continue
        if isinstance(cv, (int, float)) and not isinstance(cv, bool):
            if not math.isfinite(cv):
                log.warning(f"[settings] '{k}'={v!r} отвергнут: не конечное число")
                continue
            rng = _RANGES.get(k)
            if rng and not (rng[0] <= cv <= rng[1]):
                log.warning(f"[settings] '{k}'={cv} вне диапазона {rng}, оставляю по умолчанию")
                continue
        if k == "dash_port" and os.environ.get("PORT"):
            try:
                cv = int(os.environ["PORT"])
            except ValueError:
                pass
        vals[k] = cv
    return Settings(**vals)


# --------------------------------------------------------- печать целостности
def _hash_file(path: str) -> str:
    with open(path, "rb") as f:
        data = f.read().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def seal_manifest(directory: Optional[str] = None) -> str:
    d = directory or HERE
    lines = [f"{_hash_file(os.path.join(d, n))}  {n}" for n in CORE_FILES]
    out = os.path.join(d, MANIFEST_FILE)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return out


def verify_integrity(directory: Optional[str] = None) -> Tuple[Optional[bool], List[str]]:
    """(True|False|None, подробности). None — манифеста нет."""
    d = directory or HERE
    mp = os.path.join(d, MANIFEST_FILE)
    if not os.path.exists(mp):
        return None, [f"{MANIFEST_FILE} отсутствует"]
    expected = {}
    try:
        with open(mp, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2:
                    expected[parts[1]] = parts[0]
    except OSError as e:
        return None, [f"не прочитать манифест: {e}"]
    diffs = []
    for n in CORE_FILES:
        p = os.path.join(d, n)
        try:
            if expected.get(n) != _hash_file(p):
                diffs.append(f"{n}: ИЗМЕНЁН (хэш не совпадает с печатью)")
        except OSError:
            diffs.append(f"{n}: не читается")
    return (len(diffs) == 0), diffs


if __name__ == "__main__":
    if "--write-default" in sys.argv:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"_comment": "Меняйте только значения. Поля с '_' игнорируются.",
                       **asdict(Settings())}, f, ensure_ascii=False, indent=2)
        print("written", SETTINGS_FILE)
    elif "--seal" in sys.argv:
        print("sealed", seal_manifest())
    else:
        print(__doc__)
