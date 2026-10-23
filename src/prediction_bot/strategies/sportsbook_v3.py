from __future__ import annotations

from prediction_bot.strategies.sportsbook_v2 import SportsbookV2Strategy


class SportsbookV3Strategy(SportsbookV2Strategy):
    name = "sportsbook_v3"
    rules_version = "v3"
