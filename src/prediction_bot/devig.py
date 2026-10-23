from __future__ import annotations

from decimal import Decimal, localcontext

DEVIG_METHODS = ("multiplicative", "additive", "power", "shin")


def _implied(decimal_odds: dict[str, Decimal]) -> dict[str, Decimal]:
    if not 2 <= len(decimal_odds) <= 3 or any(price <= 0 for price in decimal_odds.values()):
        raise ValueError("devig requires two or three positive decimal odds")
    return {outcome: Decimal(1) / price for outcome, price in decimal_odds.items()}


def _power_value(probability: Decimal, exponent: Decimal) -> Decimal:
    return (probability.ln() * exponent).exp()


def _power_devig(implied: dict[str, Decimal]) -> dict[str, Decimal]:
    values = list(implied.values())
    total = sum(values, Decimal(0))
    if total == 1:
        return implied.copy()
    low, high = (Decimal(1), Decimal(2)) if total > 1 else (Decimal(0), Decimal(1))
    if total > 1:
        while sum((_power_value(p, high) for p in values), Decimal(0)) > 1:
            high *= 2
    for _ in range(120):
        mid = (low + high) / 2
        if sum((_power_value(p, mid) for p in values), Decimal(0)) > 1:
            low = mid
        else:
            high = mid
    exponent = (low + high) / 2
    return {outcome: _power_value(p, exponent) for outcome, p in implied.items()}


def _shin_value(probability: Decimal, total: Decimal, z: Decimal) -> Decimal:
    numerator = (z * z + 4 * (1 - z) * probability * probability / total).sqrt() - z
    return numerator / (2 * (1 - z))


def _shin_devig(implied: dict[str, Decimal]) -> dict[str, Decimal]:
    total = sum(implied.values(), Decimal(0))
    if total == 1:
        return implied.copy()
    if total > 1:
        low, high = Decimal(0), Decimal("0.999999999999999999")
    else:
        low, high = Decimal("-0.999999999999999999"), Decimal(0)
    for _ in range(120):
        mid = (low + high) / 2
        probability_sum = sum((_shin_value(p, total, mid) for p in implied.values()), Decimal(0))
        if probability_sum > 1:
            low = mid
        else:
            high = mid
    z = (low + high) / 2
    return {outcome: _shin_value(p, total, z) for outcome, p in implied.items()}


def devig_all(decimal_odds: dict[str, Decimal]) -> dict[str, dict[str, Decimal]]:
    """Return the valid method probabilities for a two-way or three-way book."""
    with localcontext() as context:
        context.prec = 40
        implied = _implied(decimal_odds)
        total = sum(implied.values(), Decimal(0))
        result = {
            "multiplicative": {
                outcome: probability / total for outcome, probability in implied.items()
            },
            "power": _power_devig(implied),
            "shin": _shin_devig(implied),
        }
        margin = total - 1
        additive = {
            outcome: probability - margin / Decimal(len(implied))
            for outcome, probability in implied.items()
        }
        if all(probability > 0 for probability in additive.values()):
            result["additive"] = additive
        return {method: result[method] for method in DEVIG_METHODS if method in result}


def longshot_haircut(fair: Decimal, hours_until_start: Decimal, mode: str = "slope") -> Decimal:
    if not Decimal(0) < fair < Decimal("0.5") or mode == "none":
        return fair
    if mode == "flat":
        return max(Decimal(0), fair - Decimal("0.01")) if fair < Decimal("0.20") else fair
    if mode != "slope":
        raise ValueError(f"unknown longshot haircut mode: {mode}")
    beta = (
        Decimal(1)
        if hours_until_start < 48
        else Decimal("1.04")
        if hours_until_start < 168
        else Decimal("1.24")
    )
    logit = (fair / (1 - fair)).ln()
    return Decimal(1) / (Decimal(1) + (-beta * logit).exp())
