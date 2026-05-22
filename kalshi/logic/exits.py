#!/usr/bin/env python3
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


MaybeAwaitable = Any | Awaitable[Any]


@dataclass
class LimitWalkResult:
    filled: bool
    fill_price: float | None = None
    order: dict[str, Any] | None = None
    attempts: int = 0
    reason: str = ""


class LimitWalker:
    """Places a resting limit order, cancels, and walks it one tick at a time."""

    def __init__(
        self,
        *,
        venue: str,
        side: str,
        contracts: int,
        tick_size: float,
        chase_interval: float,
        max_steps: int,
        logger: Callable[[str], None],
        best_price: Callable[[], MaybeAwaitable],
        place_order: Callable[[float], MaybeAwaitable],
        cancel_order: Callable[[dict[str, Any]], MaybeAwaitable],
        is_filled: Callable[[dict[str, Any], float], MaybeAwaitable],
    ) -> None:
        self.venue = venue
        self.side = side
        self.contracts = contracts
        self.tick_size = max(0.01, tick_size)
        self.chase_interval = max(0.1, chase_interval)
        self.max_steps = max(1, max_steps)
        self.logger = logger
        self.best_price = best_price
        self.place_order = place_order
        self.cancel_order = cancel_order
        self.is_filled = is_filled

    async def run(self) -> LimitWalkResult:
        last_order: dict[str, Any] | None = None
        last_price: float | None = None
        for attempt in range(1, self.max_steps + 1):
            best = await self._maybe(self.best_price())
            if best is None:
                return LimitWalkResult(
                    filled=False,
                    order=last_order,
                    attempts=attempt - 1,
                    reason=f"{self.venue} has no {self.side} liquidity",
                )
            price = self._next_price(best, last_price)
            self.logger(
                f"LIMIT_WALK {self.venue} place {self.side} size {self.contracts:g} "
                f"limit {price:.2f} attempt {attempt}/{self.max_steps}"
            )
            order = await self._maybe(self.place_order(price))
            last_order = order if isinstance(order, dict) else {"response": order}
            last_price = price
            filled, fill_price = await self._maybe(self.is_filled(last_order, price))
            if filled:
                self.logger(f"LIMIT_WALK {self.venue} filled at {fill_price or price:.2f}")
                return LimitWalkResult(True, fill_price or price, last_order, attempt)
            await asyncio.sleep(self.chase_interval)
            filled, fill_price = await self._maybe(self.is_filled(last_order, price))
            if filled:
                self.logger(f"LIMIT_WALK {self.venue} filled at {fill_price or price:.2f}")
                return LimitWalkResult(True, fill_price or price, last_order, attempt)
            self.logger(f"LIMIT_WALK {self.venue} cancel unfilled limit {price:.2f}")
            await self._maybe(self.cancel_order(last_order))
        return LimitWalkResult(
            filled=False,
            order=last_order,
            attempts=self.max_steps,
            reason=f"{self.venue} unfilled after {self.max_steps:g} chase attempts",
        )

    def _next_price(self, best: float, last_price: float | None) -> float:
        if self.side.lower() == "sell":
            target = best if last_price is None else min(best, last_price - self.tick_size)
            return max(0.01, round(target, 2))
        target = best if last_price is None else max(best, last_price + self.tick_size)
        return min(0.99, round(target, 2))

    async def _maybe(self, value: MaybeAwaitable) -> Any:
        if isinstance(value, Awaitable):
            return await value
        return value
