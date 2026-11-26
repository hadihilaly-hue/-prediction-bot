"""The paper command's lane summary lines keep their skip reasons: Rich must not read
`[sharp_lane_game=1, ...]` as a markup tag and drop it."""

from __future__ import annotations

import io

from rich.console import Console

from prediction_bot import cli
from prediction_bot.sharp_lane import SharpLaneResult
from prediction_bot.underdog_lane import LaneResult


def test_lane_lines_print_their_skip_reasons(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    out = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=out, width=200))
    cli._print_lane("underdog lane", LaneResult(blocked={"lane_held": 2}))
    cli._print_lane(
        "sharp lane",
        SharpLaneResult(blocked={"sharp_lane_game": 1, "sharp_lane_edge": 3}, cancelled=1),
    )
    lines = out.getvalue().splitlines()
    assert lines == [
        "underdog lane: orders_placed=0 bought_at_ask=0 pulled=0 skipped=[lane_held=2]",
        "sharp lane: orders_placed=0 bought_at_ask=0 pulled=1 "
        "skipped=[sharp_lane_edge=3, sharp_lane_game=1]",
    ]
