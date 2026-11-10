from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
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
    # off: no road-dog rule; shadow: a road dog below the threshold is logged as a
    # would-trade shadow row (reason road_dog) and never traded; trade: it bypasses the
    # net-edge threshold as before.
    exp_road_dog_mode: Literal["off", "shadow", "trade"] = "shadow"
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
        "measure_feeds",
        "release_enable_feeds",
        "exp_road_dog_high_attention_events",
        "exp_road_dog_rivalries",
        mode="before",
    )
    @classmethod
    def parse_phase_four_lists(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("plan_allowance_since", mode="before")
    @classmethod
    def blank_date_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def sync_entry_window(self) -> Settings:
        if "entry_window_min_hours" in self.model_fields_set:
            self.min_lead_hours = self.entry_window_min_hours
        elif "min_lead_hours" in self.model_fields_set:
            self.entry_window_min_hours = self.min_lead_hours
        return self

    @field_validator("odds_bookmakers")
    @classmethod
    def require_odds_bookmakers(cls, value: list[str]) -> list[str]:
        bookmakers = [book.strip() for book in value if book.strip()]
        if not bookmakers:
            raise ValueError("odds_bookmakers must contain at least one bookmaker")
        return bookmakers

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
    # v5: entries below 25c were reliably negative against the close (docs/CHANGES_v5.md);
    # rows under the floor are still logged as `price_floor` near misses.
    v2_price_floor: Decimal = Decimal("0.25")
    v2_price_ceiling: Decimal = Decimal("0.90")
    v2_max_spread: Decimal = Decimal("0.04")
    min_books: int = 2
    min_books_by_sport: dict[str, int] = Field(
        default_factory=lambda: {"americanfootball_ncaaf": 3}
    )
    max_book_spread: Decimal = Decimal("0.03")
    longshot_haircut: Literal["slope", "flat", "none"] = "slope"
    # conservative: for the side bought, the lower of the power and Shin fairs (power
    # puts a favourite 0.3-0.6c above Shin).
    v3_devig_method: Literal["power", "shin", "worst_case", "conservative"] = "conservative"
    v3_longshot_haircut: Literal["off", "slope", "flat", "none"] = "off"
    v3_allow_soft_reference: bool = True
    # v5: the v3 net-edge threshold is multiplied by this below `v3_low_price_below`.
    v3_low_price_threshold_multiplier: Decimal = Decimal("2.0")
    v3_low_price_below: Decimal = Decimal("0.40")
    v3_large_gap_mode: Literal["reverify", "block"] = "reverify"
    v3_large_gap_max: Decimal = Decimal("0.12")
    v3_min_open_interest: Decimal = Decimal("0")
    v3_min_ask_depth: Decimal = Decimal("50")
    v3_max_book_age_hours: Decimal = Decimal("3")
    v3_single_sharp_cohort: Literal["off", "shadow"] = "shadow"
    v3_dq_stop_fraction: Decimal = Decimal("0.5")
    v3_no_reentry: bool = True
    v3_execution: Literal["taker", "maker_first", "auto"] = "taker"
    v3_maker_gate_min_positions: int = 50
    v3_maker_gate_min_scored: int = 50
    v3_maker_gate_min_filled_clv: int = 20
    # v5 Phase 4: the gate that decides is queue-model maker fills with clean CLV.
    v3_maker_gate_min_queue_fills: int = 50
    v3_maker_cancel_move: Decimal = Decimal("0.01")
    # Maker shadow feed: fair - maker price - maker fee needed to place a shadow.
    v3_maker_shadow_min_edge: Decimal = Decimal("0.01")
    v3_maker_ttl_minutes: int = Field(
        default=480,
        description=(
            "Maker order TTL; lower to 30-60 minutes if sportsbook reference cadence increases."
        ),
    )
    v2_kelly_fraction: Decimal = Decimal("0.25")
    # v5 Phase 7.2: `flat` stakes one unit (`max_order_notional`) per entry; `kelly`
    # sizes quarter-Kelly on an edge capped at half the cohort's mean closing-line value
    # once that cohort is "supported" (n >= 50, 95% CI > 0), and is flat until then.
    sizing_mode: Literal["flat", "kelly"] = "flat"
    # Pre-entry injury gate (ESPN feed, NFL only for now). off = no fetch;
    # log = write injury_gate blocks but let trades through tagged
    # injury_gate_would_block; block = actually block injury_uncertain_own.
    # Flip to block only once the tagged cohort shows worse CLV with >=20 fills.
    injury_gate: Literal["off", "log", "block"] = "off"
    # Opponent uncertainty is as likely to help as hurt: it only logs unless
    # this is on (and the gate is in block mode).
    injury_gate_block_opponent: bool = False
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
    odds_planner: Literal["fixed", "event_driven"] = "fixed"
    odds_feed_selection: Literal["rotation", "lead_aware", "legacy"] = "rotation"
    odds_measurement_windows: bool = False
    odds_weekday_prior: bool = True
    odds_trigger_daily_cap: int = 4
    # Credits 2.3: a move trigger needs an ask to move at least this much since the
    # feed's last pull, and fires at most once per this many minutes.
    odds_trigger_min_move: Decimal = Decimal("0.02")
    odds_trigger_min_gap_minutes: int = 180
    # Credit cadence: 'calendar' blends the static day weights in
    # cadence_calendar.json with the daily /events count; 'off' uses static
    # weights only (no events calls); 'legacy' is the old flat share. See
    # docs/injury_feeds.md-era cadence brief; dashboard section lands in PR 2.
    cadence_mode: Literal["calendar", "legacy", "off"] = "calendar"
    cadence_calendar_path: Path = Path("data/cadence_calendar.json")
    # One-off manual bumps: "YYYY-MM-DD=weight[,YYYY-MM-DD=weight]".
    cadence_day_override: str = ""

    # Paper trading
    paper_db_path: Path = Path("data/paper.sqlite")
    paper_starting_cash: Decimal = Decimal("1000")
    clv_max_calls_scanner: int = 300

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
    # v5 entry window, in hours before kickoff. `min_lead_hours` follows
    # `entry_window_min_hours` (setting either one sets both); the max also bounds feed
    # planning's lookahead. 0 disables a bound.
    entry_window_min_hours: int = 72
    entry_window_max_hours: int = 168
    # Feeds that never qualify for the entry window but get shadow-only pulls at T-6h
    # and T-90m of their earliest Kalshi game, capped at `measure_daily_credit_cap`
    # credits per UTC day across all of them.
    measure_feeds: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["icehockey_nhl", "basketball_nba", "basketball_ncaab"]
    )
    measure_daily_credit_cap: int = 2
    # v5 Phase 3 per-game pull targets (T-24h/T-6h/T-90m/T-10m). `hybrid` keeps the
    # rotation for 72h+ entry pulls and lets the plan spend the rest of each day's
    # budget; `odds_entry_share` is the rotation's reserved share.
    odds_pull_plan: Literal["off", "hybrid"] = "hybrid"
    # v5 Phase 5: Polymarket as a second, read-only reference price (never traded).
    # The tag slugs are Polymarket's sport tags as best known; unverified from here.
    polymarket_enabled: bool = True
    polymarket_gamma_base: str = "https://gamma-api.polymarket.com"
    polymarket_clob_base: str = "https://clob.polymarket.com"
    polymarket_max_requests: int = 200
    polymarket_tags: dict[str, str] = Field(
        default_factory=lambda: {
            "americanfootball_nfl": "nfl",
            "americanfootball_ncaaf": "cfb",
            "basketball_nba": "nba",
            "basketball_ncaab": "cbb",
            "icehockey_nhl": "nhl",
            "baseball_mlb": "mlb",
            "soccer_epl": "epl",
            "soccer_spain_la_liga": "la-liga",
            "soccer_italy_serie_a": "serie-a",
            "soccer_germany_bundesliga": "bundesliga",
            "soccer_france_ligue_one": "ligue-1",
            "soccer_uefa_champs_league": "ucl",
            "soccer_usa_mls": "mls",
            "mma_mixed_martial_arts": "ufc",
        }
    )
    odds_entry_share: float = 0.5
    # v5 Phase 3-E: information-release calendar for plan targets (clock entries such
    # as NFL status reports, kickoff entries such as lineups). `off` reproduces the
    # Phase 3 T-24h/T-6h/T-90m/T-10m targets exactly.
    odds_release_calendar: Literal["off", "on"] = "on"
    release_calendar_path: Path = Path("data/release_calendar.json")
    # Feeds whose calendar entries are disabled in the file but should run (RESERVED:
    # NBA ships disabled; `basketball_nba` here turns its 13:35/17:35 ET pulls on).
    release_enable_feeds: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # `rotation` (default) keeps the 72h+ entry rotation on sportsbook slots;
    # `fallback` (RESERVED) drops it, gives the plan the whole day budget and adds one
    # 15:00 ET `grid` target per eligible feed on days with no other target.
    odds_grid: Literal["rotation", "fallback"] = "rotation"
    # Credits per month kept for move-trigger pulls (`odds_triggers`): plan targets
    # never spend them, and the rotation paces as if they were already spent.
    odds_move_reserve: int = 50
    # Restart the pull plan's month-to-date allowance from this UTC date (inside the
    # current month), so a mid-month ODDS_ENTRY_SHARE change is not applied to the
    # credits the plan already spent. Ignored in other months.
    plan_allowance_since: date | None = None

    # Fees come from each series' Kalshi schedule (fees.fee_for_ticker); only the
    # rounding (`fee_rounding`) is configurable.

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
