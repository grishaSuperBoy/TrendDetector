"""
states.py — читаемые состояния монеты и рынка.

Чистые функции без побочных эффектов. Никакого I/O.

CoinState: 7 состояний монеты.
MarketState: 5 состояний рынка (считается по распределению CoinState).
"""
from enum import Enum
from typing import Dict, List, Optional


class CoinState(str, Enum):
    FLAT        = "flat"
    ACC_LONG    = "acc_long"
    ACC_SHORT   = "acc_short"
    TREND_UP    = "trend_up"
    TREND_DOWN  = "trend_down"
    SQUEEZE_UP  = "squeeze_up"
    SQUEEZE_DOWN = "squeeze_down"


class MarketState(str, Enum):
    CHOP         = "chop"
    LONG_TREND   = "long_trend"
    SHORT_TREND  = "short_trend"
    SQUEEZE_RISK = "squeeze_risk"
    MIXED        = "mixed"


def coin_state(
    toxic_1h: float,
    flow_dir: int,
    vol_ok: bool,
    ret_30s: Optional[float],
    obi_ok: bool,
    oi_trend: Optional[float],
    P: dict,
) -> CoinState:
    """
    Определяет состояние одной монеты.

    Параметры P:
        alert    — минимальный toxic_1h для «активности» (float, дефолт 0.70)
        min_ret  — минимальный |ret_30s| в bps для TREND (float, дефолт 4.0)
        oi_drop  — порог падения oi_trend_15m для SQUEEZE (float, дефолт -1.0)

    Иерархия решений:
        1. Нет объёма или toxic ниже порога → FLAT
        2. Оба условия trend_fuel + OI падает → SQUEEZE
        3. Все условия trend_fuel → TREND
        4. Иначе (toxic есть, но цена/OBI/OI неполные) → ACC
    """
    alert   = float(P.get("alert", 0.70))
    min_ret = float(P.get("min_ret", 4.0))
    oi_drop = float(P.get("oi_drop", -1.0))

    # --- FLAT: нет объёма или нет токсичности ---
    if not vol_ok or toxic_1h < alert or flow_dir == 0:
        return CoinState.FLAT

    # --- проверяем наличие тренда: цена + OBI в сторону потока ---
    price_ok = False
    if ret_30s is not None:
        r = float(ret_30s)
        if flow_dir > 0 and r >= min_ret:
            price_ok = True
        elif flow_dir < 0 and r <= -min_ret:
            price_ok = True

    # --- SQUEEZE: всё как для TREND, но OI падает ---
    if price_ok and obi_ok:
        if oi_trend is not None and oi_trend < oi_drop:
            return CoinState.SQUEEZE_UP if flow_dir > 0 else CoinState.SQUEEZE_DOWN
        return CoinState.TREND_UP if flow_dir > 0 else CoinState.TREND_DOWN

    # --- ACCUMULATION: токсичность есть, но движения нет ---
    return CoinState.ACC_LONG if flow_dir > 0 else CoinState.ACC_SHORT


def market_state(states: List[CoinState], P: dict) -> MarketState:
    """
    Определяет состояние рынка по списку CoinState всех монет.

    Параметры P:
        trend_frac   — доля TREND_* для LONG/SHORT_TREND (float, дефолт 0.4)
        squeeze_frac — доля SQUEEZE_* для SQUEEZE_RISK (float, дефолт 0.3)
    """
    trend_frac   = float(P.get("trend_frac", 0.4))
    squeeze_frac = float(P.get("squeeze_frac", 0.3))

    total = len(states)
    if total == 0:
        return MarketState.CHOP

    up_count  = sum(1 for s in states if s in (CoinState.TREND_UP,))
    dn_count  = sum(1 for s in states if s in (CoinState.TREND_DOWN,))
    sqz_count = sum(1 for s in states if s in (CoinState.SQUEEZE_UP, CoinState.SQUEEZE_DOWN))

    up_frac  = up_count  / total
    dn_frac  = dn_count  / total
    sqz_frac = sqz_count / total

    # SQUEEZE_RISK имеет приоритет над трендом
    if sqz_frac >= squeeze_frac:
        return MarketState.SQUEEZE_RISK

    if up_frac >= trend_frac and up_frac > dn_frac:
        return MarketState.LONG_TREND

    if dn_frac >= trend_frac and dn_frac > up_frac:
        return MarketState.SHORT_TREND

    # Смешанный — и лонг и шорт тренды значимые, но не доминируют
    if up_frac > 0.1 and dn_frac > 0.1:
        return MarketState.MIXED

    return MarketState.CHOP
