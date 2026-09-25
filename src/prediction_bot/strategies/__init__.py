from __future__ import annotations

from prediction_bot.strategies.base import Strategy
from prediction_bot.strategies.book_scanner import BookScannerStrategy
from prediction_bot.strategies.sportsbook_arb import SportsbookArbStrategy

STRATEGIES: dict[str, type[Strategy]] = {
    BookScannerStrategy.name: BookScannerStrategy,
    SportsbookArbStrategy.name: SportsbookArbStrategy,
}

__all__ = ["STRATEGIES", "BookScannerStrategy", "SportsbookArbStrategy", "Strategy"]
