from __future__ import annotations

from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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
    rules_version: Literal["v1", "v2", "v3"] = "v1"
    shadow_strategies: Annotated[list[str], NoDecode] = Field(default_factory=list)
    v2_disabled_sports: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["basketball_nba"],
        description=(
            "Enable NBA v2 only after a game-day run following the 11 AM-1 PM local injury "
            "report, or with an injury feed, and a same-day kickoff guard."
        ),
    )
    exp_line_move_points: Decimal = Decimal("0.03")
    exp_road_dog_enabled: bool = True
    exp_road_dog_position_bankroll_fraction: Decimal = Decimal("0.01")
    exp_road_dog_high_attention_events: Annotated[list[str], NoDecode] = Field(default_factory=list)
    exp_road_dog_rivalries: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("shadow_strategies", mode="before")
    @classmethod
    def parse_shadow_strategies(cls, value: object) -> object:
        if isinstance(value, str):
            return [name.strip() for name in value.split(",") if name.strip()]
        return value

    @field_validator(
        "v2_disabled_sports",
        "exp_road_dog_high_attention_events",
        "exp_road_dog_rivalries",
        mode="before",
    )
    @classmethod
    def parse_phase_four_lists(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    # V2 correctness gates
    kickoff_buffer_minutes: dict[str, int] = Field(
        default_factory=lambda: {
            "americanfootball_nfl": 30,
            "americanfootball_ncaaf": 30,
            "basketball_nba": 30,
        }
    )
    kickoff_buffer_default_minutes: int = 15
    max_gross_edge: Decimal = Decimal("0.07")
    big_edge_whitelist: list[str] = Field(default_factory=list)
    in_play_consensus_high: Decimal = Decimal("0.93")
    in_play_consensus_low: Decimal = Decimal("0.07")
    in_play_first_consensus_min: Decimal = Decimal("0.25")
    in_play_first_consensus_max: Decimal = Decimal("0.75")
    in_play_kalshi_price_min: Decimal = Decimal("0.03")
    in_play_kalshi_price_max: Decimal = Decimal("0.97")
    in_play_consensus_jump: Decimal = Decimal("0.15")
    fee_rounding: Literal["cent", "centicent"] = "cent"
    v2_min_net_edge: Decimal = Decimal("0.02")
    v2_longshot_price: Decimal = Decimal("0.25")
    v2_longshot_min_gross: Decimal = Decimal("0.04")
    v2_price_floor: Decimal = Decimal("0.10")
    v2_price_ceiling: Decimal = Decimal("0.90")
    v2_max_spread: Decimal = Decimal("0.04")
    min_books: int = 2
    min_books_by_sport: dict[str, int] = Field(
        default_factory=lambda: {"americanfootball_ncaaf": 3}
    )
    max_book_spread: Decimal = Decimal("0.03")
    longshot_haircut: Literal["slope", "flat", "none"] = "slope"
    v3_devig_method: Literal["power", "shin", "worst_case"] = "power"
    v3_longshot_haircut: Literal["off", "slope", "flat", "none"] = "off"
    v3_allow_soft_reference: bool = True
    v3_large_gap_mode: Literal["reverify", "block"] = "reverify"
    v3_large_gap_max: Decimal = Decimal("0.12")
    v3_min_open_interest: Decimal = Decimal("100")
    v3_max_book_age_hours: Decimal = Decimal("3")
    v3_dq_stop_fraction: Decimal = Decimal("0.5")
    v3_no_reentry: bool = True
    v2_kelly_fraction: Decimal = Decimal("0.25")
    v2_position_bankroll_fraction: Decimal = Decimal("0.02")
    v2_event_bankroll_fraction: Decimal = Decimal("0.02")
    v2_daily_bankroll_fraction: Decimal = Decimal("0.10")
    v2_total_open_bankroll_fraction: Decimal = Decimal("0.30")

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
