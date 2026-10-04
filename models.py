"""Компактные модели данных. Только то, что реально используется."""
from dataclasses import dataclass
from typing import List, Tuple


@dataclass(slots=True)
class OrderBookDepth5:
    """Top-N стакан одной биржи."""
    exchange: str
    symbol: str
    ts: float                    # локальный epoch (сек)
    exchange_ts: int             # epoch биржи (мс)
    bids: List[Tuple[float, float]]
    asks: List[Tuple[float, float]]

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    @property
    def mid_price(self) -> float:
        bb, ba = self.best_bid, self.best_ask
        return (bb + ba) / 2.0 if (bb > 0 and ba > 0) else 0.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid_price
        if mid <= 0 or not self.bids or not self.asks:
            return 0.0
        return (self.best_ask - self.best_bid) / mid * 10000.0

    @property
    def bid_vol_5(self) -> float:
        return sum(q for _, q in self.bids[:5])

    @property
    def ask_vol_5(self) -> float:
        return sum(q for _, q in self.asks[:5])

    @property
    def depth_bid_usd(self) -> float:
        return sum(p * q for p, q in self.bids[:5])

    @property
    def depth_ask_usd(self) -> float:
        return sum(p * q for p, q in self.asks[:5])

    @property
    def obi(self) -> float:
        """(BidVol - AskVol) / (BidVol + AskVol) ∈ [-1, +1]."""
        bv, av = self.bid_vol_5, self.ask_vol_5
        tot = bv + av
        if tot <= 1e-9:
            return 0.0
        return (bv - av) / tot


@dataclass(slots=True)
class LiquidationEvent:
    """Принудительная ликвидация (Binance !forceOrder@arr)."""
    id: str
    symbol: str
    side: str          # 'BUY' (шорт-ликвидация) или 'SELL' (лонг-ликвидация)
    price: float
    qty: float
    qty_usd: float
    exchange: str
    ts: float
