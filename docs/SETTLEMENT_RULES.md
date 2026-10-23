# Settlement rules

The v3 sportsbook strategy checks each market's `rules_primary` against the phrases
below before pricing it. Checks are case-insensitive and normalize whitespace. A
missing rule entry blocks as `settlement_rule_unknown`; a missing required phrase or
missing soccer draw outcome blocks as `settlement_rule_mismatch`.

| Kalshi series | Odds market | Kalshi settlement |
| --- | --- | --- |
| NFL, NCAAF, NBA, NCAAB, NHL, MLB game series | `h2h` | The named team wins the game, including overtime; MLB includes extra innings and NHL includes a shootout. |
| EPL, La Liga, UCL, Serie A, Bundesliga, Ligue 1, UEL, UECL, MLS, Liga MX game series | `h2h` | Three-way home/away/draw result after 90 minutes plus stoppage time; extra time and penalties do not count. |
| UFC fight series | `h2h` | The named fighter wins the fight. |
| Masters, PGA Championship, U.S. Open, The Open | `outrights` | The named golfer wins the tournament. |

Soccer contracts use a three-way result (home, away, TIE). The v3 strategy will not
price a two-team Kalshi event from a two-way Odds line: both the Kalshi TIE leg and
an Odds consensus Draw probability are required. Odds API soccer `h2h` is a 90-minute
three-way market. U.S. sports `h2h` includes overtime, extra innings, or a shootout
where applicable.

For soccer, a match cancelled or rescheduled more than 48 hours after its original
start resolves at fair price. NFL, NCAAF, NBA, NHL, and MLB markets stay open when a
postponed game starts within 48 hours (within two days for MLB); otherwise they
resolve at fair price.

No open or settled markets were available for the four golf outright series during
implementation. Their configured `wins` phrase is provisional rather than verified
against a live market. The sportsbook strategy does not support these outright series,
so these entries do not enable golf trading.
