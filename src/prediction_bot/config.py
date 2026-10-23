from __future__ import annotations

from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

KALSHI_DEMO_REST = "https://external-api.demo.kalshi.co/trade-api/v2"
KALSHI_DEMO_WS = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
KALSHI_PROD_REST = "https://api.elections.kalshi.com/trade-api/v2"


class Mode(str, Enum):
    paper = "paper"
    live = "live"


class Settings(BaseSettings):
    """All runtime configuration. Loaded from env vars / .env with prefix PBOT_."""

    model_config = SettingsConfigDict(env_prefix="PBOT_", env_file=".env", extra="ignore")

    mode: Mode = Mode.paper

    # Kalshi
    kalshi_api_base: str = KALSHI_DEMO_REST
    kalshi_api_key_id: str | None = None
    kalshi_private_key_path: Path | None = None
    kalshi_private_key_pem: str | None = None  # inline PEM; alternative to the path

    # Sportsbook odds (https://the-odds-api.com)
    odds_api_key: str | None = None
    odds_api_base: str = "https://api.the-odds-api.com/v4"
    # Sharp books first: Pinnacle/Circa/BetOnline post early lines and carry most of the
    # signal at the multi-day lead times we enter; see odds.BOOK_WEIGHTS.
    odds_bookmakers: list[str] = Field(
        default_factory=lambda: ["pinnacle", "circa", "betonlineag", "draftkings", "fanduel"]
    )
    # Scheduled runs per day. When > 0, each run spends at most its even share of the
    # Odds API requests left this month, on the leagues with the most games starting
    # within `odds_lookahead_hours`. 0 = no budget.
    odds_runs_per_day: float = 0
    odds_lookahead_hours: float = 36

    # Paper trading
    paper_db_path: Path = Path("data/paper.sqlite")
    paper_starting_cash: Decimal = Decimal("1000")

    # Risk limits (apply to both paper and live)
    max_order_notional: Decimal = Decimal("25")
    max_position_contracts: Decimal = Decimal("100")
    min_edge: Decimal = Decimal("0.03")  # minimum expected edge (prob points) after fees
    # Hard stops owned by code, never by a strategy: the kill switch halts every new entry
    # (and cancels resting live orders); the daily-loss limit halts entries for the rest of
    # the UTC day once realized PnL settled today reaches -limit. Unset = disabled.
    kill_switch: bool = False
    max_daily_loss: Decimal | None = None
    # Entry style: "maker" posts at bid+1c inside the spread when it exists (1.75% fee
    # and a better price than the ask), "taker" always crosses to the ask (7% fee).
    entry_style: Literal["taker", "maker"] = "maker"
    # Entries are skipped when the top of book holds fewer than this many contracts on
    # the level we'd interact with — a thinner book is usually a stale/dead quote.
    min_book_depth: Decimal = Decimal("10")
    # `underdog_value` only buys sides whose entry price sits in this band (10c..25c =
    # 4x..10x). Paper results: <=24c entries won 4/7 vs 1/7 at 28-40c, so the top of the
    # old band is where "edge vs consensus" was noise rather than value.
    underdog_min_price: Decimal = Decimal("0.10")
    underdog_max_price: Decimal = Decimal("0.25")
    # Sportsbook strategies only enter at least this many hours before the event
    # resolves: paper results show same-day entries in efficient markets lose while
    # early entries keep their edge. 0 disables the check.
    min_lead_hours: int = 72

    # Fees (Kalshi general schedule: fee = mult * C * P * (1-P), rounded up to the cent)
    taker_fee_multiplier: Decimal = Decimal("0.07")
    maker_fee_multiplier: Decimal = Decimal("0.0175")

    @property
    def is_live(self) -> bool:
        return self.mode == Mode.live

    @property
    def is_demo_venue(self) -> bool:
        return "demo" in self.kalshi_api_base

    @property
    def has_kalshi_credentials(self) -> bool:
        return bool(
            self.kalshi_api_key_id and (self.kalshi_private_key_path or self.kalshi_private_key_pem)
        )
