from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

ClvInput = Decimal | float | int | str


def weighted_position_clv(values: Iterable[tuple[ClvInput, ClvInput]]) -> Decimal | None:
    weighted = [
        (Decimal(str(clv)), Decimal(str(count))) for clv, count in values if Decimal(str(count)) > 0
    ]
    total = sum((count for _, count in weighted), Decimal(0))
    if total == 0:
        return None
    return sum((clv * count for clv, count in weighted), Decimal(0)) / total
