# Injury feeds — ESPN endpoint probe (2026-10-25)

Endpoint: `https://site.api.espn.com/apis/site/v2/sports/{sport_path}/injuries`.
Free, no API key, no The Odds API credits. Probed once per path; `InjuryClient`
memoizes per run so each sport costs one request per paper cycle.

| Odds sport key | ESPN path | Result |
|---|---|---|
| americanfootball_nfl | football/nfl | **Verified.** 32 teams, current entries (displayName == our `odds_names[0]` exactly — see `espn_names` in teams.json). Statuses seen: Out, Active, Injured Reserve, Questionable, Suspension, Doubtful. |
| americanfootball_ncaaf | football/college-football | **Stale.** Only 3 teams returned, entries dated 2020–2022. Useless for CFB; we use the `line_move_proxy` (4¢+ adverse consensus move before kickoff) instead. |
| basketball_nba | basketball/nba | Live data returned (27 teams). Unverified for gating — no `espn_names` aliases yet, NBA stays disabled anyway. |
| icehockey_nhl | hockey/nhl | Live data returned (29 teams). Unverified — no `espn_names` aliases yet. |
| baseball_mlb | baseball/mlb | Live data returned (30 teams). Unverified — no `espn_names` aliases yet. |
| soccer_epl | soccer/eng.1 | Empty list (0 teams). Treated as no feed. |

Rules that follow from this:

- `ESPN_VERIFIED_SPORTS = {americanfootball_nfl}` — the only sport with both a
  live feed and team aliases. Everything else is skipped by the gate
  (`injury_gate_skipped` in `blocks.detail`) and stays `unknown` in fill tagging.
- Rate limit: none seen on first probes; status codes are logged per fetch. If
  it ever returns 429, back off to one fetch per sport per 6 hours.
- Never scrape news sites for CFB injuries — there is no league-wide report.
