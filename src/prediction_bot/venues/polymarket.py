"""Read-only Polymarket client (v5 Phase 5): Gamma for discovery, CLOB `/book` for mids.

No key, no orders. The response shapes below follow Polymarket's public Gamma and CLOB
APIs as documented at the time of writing, but they were not verified from this
environment (its network policy blocks both hosts), so every field is parsed
defensively and anything unexpected is skipped rather than guessed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

log = logging.getLogger(__name__)
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


@dataclass(frozen=True)
class PolyMarket:
    """One Gamma market: its outcome names, CLOB token ids and game start."""

    question: str
    outcomes: tuple[str, ...]
    token_ids: tuple[str, ...]
    subject: str  # groupItemTitle when present (the team or "Draw" a Yes/No market is about)
    start: datetime | None


@dataclass(frozen=True)
class PolyEvent:
    title: str
    start: datetime | None
    markets: tuple[PolyMarket, ...]


def _json_list(value: object) -> list[str]:
    """Gamma returns some list fields as JSON-encoded strings."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace(" ", "T").replace("Z", "+00:00")
    if text.endswith("+00"):
        text += ":00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def parse_event(raw: dict[str, Any]) -> PolyEvent:
    markets = []
    for market in raw.get("markets") or []:
        if not isinstance(market, dict) or market.get("closed") is True:
            continue
        outcomes = _json_list(market.get("outcomes"))
        tokens = _json_list(market.get("clobTokenIds"))
        if not outcomes or len(outcomes) != len(tokens):
            continue
        markets.append(
            PolyMarket(
                question=str(market.get("question") or ""),
                outcomes=tuple(outcomes),
                token_ids=tuple(tokens),
                subject=str(market.get("groupItemTitle") or ""),
                start=_time(market.get("gameStartTime")) or _time(market.get("startDate")),
            )
        )
    return PolyEvent(
        title=str(raw.get("title") or ""),
        start=_time(raw.get("startTime")) or _time(raw.get("startDate")),
        markets=tuple(markets),
    )


def parse_book(raw: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    """Best bid and best ask from a CLOB `/book` response."""

    def prices(side: str) -> list[Decimal]:
        out = []
        for level in raw.get(side) or []:
            try:
                out.append(Decimal(str(level["price"])))
            except (KeyError, TypeError, InvalidOperation):
                continue
        return out

    bids, asks = prices("bids"), prices("asks")
    return (max(bids) if bids else None, min(asks) if asks else None)


class PolymarketClient:
    def __init__(
        self,
        gamma_base: str = GAMMA_BASE,
        clob_base: str = CLOB_BASE,
        client: httpx.Client | None = None,
    ) -> None:
        self.gamma_base = gamma_base.rstrip("/")
        self.clob_base = clob_base.rstrip("/")
        self._client = client or httpx.Client(timeout=15.0)
        self.requests = 0

    def _get(self, url: str, params: dict[str, Any]) -> Any:
        self.requests += 1
        resp = self._client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def events(self, tag_slug: str, *, limit: int = 100, max_pages: int = 3) -> list[PolyEvent]:
        out: list[PolyEvent] = []
        for page in range(max_pages):
            data = self._get(
                f"{self.gamma_base}/events",
                {
                    "tag_slug": tag_slug,
                    "closed": "false",
                    "active": "true",
                    "limit": limit,
                    "offset": page * limit,
                },
            )
            if isinstance(data, dict):
                data = data.get("data", [])
            rows = data if isinstance(data, list) else []
            out.extend(parse_event(row) for row in rows if isinstance(row, dict))
            if len(rows) < limit:
                break
        return out

    def book(self, token_id: str) -> tuple[Decimal | None, Decimal | None]:
        return parse_book(self._get(f"{self.clob_base}/book", {"token_id": token_id}))
