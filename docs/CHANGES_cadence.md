# Credit cadence (v5)

The paper-trading loop gets ~500 Odds API credits a month. Until now the pacer
spread them as a flat share of every remaining slot; the cadence system moves
them onto the days that feed the 72–168h entry window. The GitHub cron is
unchanged (12 slots/day) — the pacer decides how many feeds (0–3) each slot
pulls. Rollback: set the `PBOT_CADENCE_MODE` repo variable to `legacy`.

## Algorithm

Dates in `data/cadence_calendar.json` are **game dates**; pull windows are the
game date shifted back by `entry_window_hours / 24` (3–7 days).

```
static(d) = base(d) × cluster(d) × deadzone(d)
base(d)   = Σ over active sports of weekly_profile weight for pull weekday d
            (sport active ⇔ some game day in [d+3, d+7] is inside one of its
             season ranges and outside its blackouts)
cluster(d)   = cluster weight if a cluster's pull window contains d, else 1
deadzone(d)  = 0.5 inside a dead zone, else 1

dyn(d)  = count of mapped games commencing on dates in [d+3, d+7], from one
          free /events call per active sport per day covering [today+3, today+13]
weight(d) = static(d) × (0.5 + 0.5 × dyn(d)/median_dyn), clamped to static×1.5

budget(today) = remaining × weight(today) / Σ weight(remaining days of month)
remaining     = credits_left × 0.95   (5% reserve for manual runs/retries)
per-slot      = budget / slots_left_today, carried as a fraction
feeds/slot    = round(carried credit): <0.5 → 0, ≥1.5 → 2, ≥2.5 → 3 (max 3)
```

Floors/caps: ≥2 pulls/day while any sport is active, ≤40/day. On/after the
20th, a projected month-end under-spend >15% applies a breadth multiplier —
the day's allocation doubles (still under the 40/day cap) so slots pull extra
feeds.

Notes on the interpretation:

- `dyn` buckets games by commence **date** inside each pull day's window, so a
  game counts toward every pull day that can still reach it in 72–168h.
- `soccer_uefa_*` sports use the `soccer_uefa_midweek` profile, other `soccer_*`
  use `soccer_weekend_leagues`; active sports with no profile row (golf)
  contribute weight 1.0.
- Days beyond the 7-day events horizon use static only; a zero-mapped day still
  gets half its static share; `median_dyn = 0` leaves static alone.
- `/events` freedom is verified per run by comparing `x-requests-used` before
  and after the first call; if it rises, the dyn layer is dropped and the plan
  runs on static alone (the run still gets its static budget).
- Note-only `seasons` entries (UEL/UECL derived-date notes) are skipped — no
  concrete range, so those sports never activate.

## Modes

`PBOT_CADENCE_MODE`:

- `calendar` (default): the full static+dyn blend above.
- `off`: static weights only — no `/events` calls at all.
- `legacy`: the old flat share (`remaining / runs_left_in_month`), preserved so
  a revert is a one-line env change; `test_legacy_mode_reproduces_run_share`
  pins it to the exact old numbers.

`PBOT_CADENCE_DAY_OVERRIDE` takes comma-separated `YYYY-MM-DD=weight` pairs to
replace a day's blended weight by hand (e.g. a slate you know is thin).

Every slot upserts one `cadence_days` row:
`(day, static_w, dyn_count, dyn_json, blended_w, budget, pulled, feeds_json,
mode)` — `dyn_json` caches the day's 7-day series so the events probe runs
once a day.

## Calendar data — flagged items

`verify: true` entries in the committed calendar, unconfirmed at commit time:

- **MLB**: treated as off until a 2027 CBA is confirmed (Opening Day Mar 24 2027
  is conditional on a labor deal).
- **NHL**: flat ~0.3 weight until `feed_stats.lead_p50_h` for NHL reaches 72 —
  books rarely post NHL lines 72h out. Playoff window (Apr 12–Jun 20) assumed.
- **MLS**: 2026 playoffs through MLS Cup (Dec date unconfirmed); 2027 sprint
  season and 2027–28 fall start are provisional.
- **NFL/NCAAF 2027** ranges and NCAAF Week 0/1 + Army-Navy dates: assumed.
- **La Liga / Serie A / Ligue 1** winter pauses and **Liga MX Clausura 2027**:
  approximate.
- **UEL/UECL**: note-only (Thursdays in UCL weeks) — skipped at load, so UEFA
  midweek weight currently comes from UCL alone.
- **UFC** dark weeks after mid-Dec and the **FIFA Sept/Oct window** merge:
  marked unverified.
- **Golf majors**: 2027 dates from venue rotation reporting, unconfirmed.

## Evidence rule

After 60 days, compare `gate_candidates` per credit and near-miss count per
credit under `calendar` against the legacy baseline in `docs/CHANGES_v4.md`.
If the number does not rise, set `PBOT_CADENCE_MODE=legacy` and revert.
