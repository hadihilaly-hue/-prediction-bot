from __future__ import annotations

from decimal import Decimal
from enum import Enum
from pathlib import Path

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
    odds_bookmakers: list[str] = Field(
        default_factory=lambda: ["pinnacle", "draftkings", "fanduel"]
    )

    # Paper trading
    paper_db_path: Path = Path("data/paper.sqlite")
    paper_starting_cash: Decimal = Decimal("1000")

    # Risk limits (apply to both paper and live)
    max_order_notional: Decimal = Decimal("25")
    max_position_contracts: Decimal = Decimal("100")
    min_edge: Decimal = Decimal("0.03")  # minimum expected edge (prob points) after fees

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
