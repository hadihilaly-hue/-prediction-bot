from __future__ import annotations

from prediction_bot.strategies.base import Strategy
from prediction_bot.strategies.book_scanner import BookScannerStrategy
from prediction_bot.strategies.sportsbook_arb import SportsbookArbStrategy
from prediction_bot.strategies.underdog_value import UnderdogValueStrategy

STRATEGIES: dict[str, type[Strategy]] = {
    BookScannerStrategy.name: BookScannerStrategy,
    SportsbookArbStrategy.name: SportsbookArbStrategy,
    UnderdogValueStrategy.name: UnderdogValueStrategy,
}

__all__ = [
    "STRATEGIES",
    "BookScannerStrategy",
    "SportsbookArbStrategy",
    "Strategy",
    "UnderdogValueStrategy",
]
