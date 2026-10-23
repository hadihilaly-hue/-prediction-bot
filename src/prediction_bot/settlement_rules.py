from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SettlementRule:
    sport_key: str
    outcome_type: str
    required_phrases: tuple[str, ...]
    odds_market: str
    needs_draw: bool


def _load_settlement_rules() -> dict[str, SettlementRule]:
    path = Path(__file__).with_name("data") / "settlement_rules.json"
    data: dict[str, dict[str, object]] = json.loads(path.read_text())
    rules: dict[str, SettlementRule] = {}
    for series, entry in data.items():
        phrases = entry["required_phrases"]
        if not isinstance(phrases, list) or not all(isinstance(phrase, str) for phrase in phrases):
            raise ValueError(f"invalid required_phrases for {series}")
        rules[series] = SettlementRule(
            sport_key=str(entry["sport_key"]),
            outcome_type=str(entry["outcome_type"]),
            required_phrases=tuple(phrases),
            odds_market=str(entry["odds_market"]),
            needs_draw=bool(entry["needs_draw"]),
        )
    return rules


def normalize_rules_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


SETTLEMENT_RULES = _load_settlement_rules()
