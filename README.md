# War Watch

Spots countries getting ready to attack Ireland by watching how their high-level players spend their skill points. War Era doesn't surface enemy preparation directly — but when citizens of a country start resetting their skills en masse, or when their combat allocation climbs day after day, it's a strong signal that mobilisation is underway.

Sibling of `alert.py` (production-bonus market notifier).

## What it watches

A dynamic watchlist of countries within strategic reach of Ireland, plus anyone Ireland's currently at war with:

- **Neighbours, up to 3 hops out.** Walks the game's region adjacency graph (`region.neighbors`) breadth-first from Ireland's territory. A country joins the watchlist if it controls any region within 3 hops. Direct attackers turn up at hop 1; countries that could plausibly project force via one or two intermediate conquests turn up at hops 2–3.
- **Active war opponents.** Anything in Ireland's `warsWith` list, regardless of distance.

Watchlist is rebuilt from live API data each run. No code change needed when territory shifts or wars start/end.

## Signals

For each watchlisted country the script samples up to 25 active high-level citizens (level ≥ 20, online in the last 14 days) and tracks three numbers:

- **New resets per run.** Each citizen's `lastSkillsResetAt` is compared to what was stored on the previous run. If it's advanced, that's a fresh reset event. Skill resets cost gold, so people don't do them casually — a burst means citizens are repurposing themselves, almost always for combat. Counted as discrete events rather than rolling windowed totals, so a single reset wave doesn't smear across days and inflate baselines.
- **Combat / economy skill ratio.** Of points spent on bucketed combat skills (attack, precision, dodge, armour, loot, crits, health) vs economy skills (companies, entrepreneurship, production, management), what fraction is combat. Median across the full sample.
- **Resetter combat ratio.** Same calculation, but median across only the citizens who reset this run. Catches the "30 people reset and they all came out at 90% combat" pattern even when the overall median barely budges.

All three go into a 14-day rolling history per country. From there:

- **Reset burst** — current new_resets count is ≥ 2σ above that country's own rolling baseline, or ≥ 3 absolute (whichever is higher).
- **Ratio creep** — combat ratio has climbed ≥ 20 percentage points since ~7 days ago.

First ~5 runs after deployment (or after a schema migration) collect data silently while baselines build up.

## Output

Three kinds of Discord messages:

- **High-severity bursts** get their own embed: ≥ 1.5× threshold or ≥ 10 new resets in a single run. Includes the resetter allocation as qualitative context (combat-skewed / balanced / economy-leaning) and sparkline trends. Per-country cooldown of 3 days between repeat urgent alerts, bypassed only if the burst escalates by 50%+ vs the last alert sent.
- **Daily digest** — single summary embed listing every flagged country with 🔴/🟠/🟡 severity icons, plus a ✅ stand-down section for countries that were flagged previously but are quiet now. Posts whenever there's anything to report, including digests that are purely stand-downs.
- **Health warnings** — separate embed if the watchlist comes back empty (critical, red) or if fewer than half of watchlisted countries can be sampled (degraded, yellow). Catches silent failures where the script runs but produces no useful data.

If nothing's flagged, nothing's stood down, and the pipeline's healthy, nothing's posted.

## Setup

```bash
pip install requests
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python war.py
```

Schedule daily (the constants are tuned for that cadence).

## Configuration

Top-of-file constants worth knowing:

| Constant | Default | Notes |
|---|---|---|
| `BORDER_HOPS` | 3 | How far to expand the watchlist from Ireland's territory |
| `SAMPLE_TOP_N` | 25 | High-level citizens sampled per country |
| `MIN_LEVEL` | 20 | Below this is too noisy to read combat intent |
| `MIN_SAMPLE` | 10 | Skip countries with fewer eligible citizens |
| `ACTIVITY_WINDOW_DAYS` | 14 | "Active" if connected within this many days |
| `DISCOVERY_INTERVAL_DAYS` | 7 | How often to re-paginate citizen lists |
| `BASELINE_SIGMA` | 2.0 | Burst sensitivity |
| `RESET_FLOOR` | 3 | Hard minimum new_resets to trigger an alert |
| `RATIO_CREEP_PP` | 20.0 | pp gain that triggers a creep alert |
| `RATIO_LOOKBACK_DAYS` | 7 | Creep lookback |
| `URGENT_COOLDOWN_DAYS` | 3 | Per-country gap between repeat urgent alerts |
| `URGENT_ESCALATION_FACTOR` | 1.5 | Bypasses cooldown if current burst is this much bigger than the last sent |
| `HEALTH_SNAPSHOT_RATE` | 0.5 | Warn if fewer than this fraction of watchlist could be sampled |

## State

`war_state.json` sits next to the script. Versioned (`STATE_VERSION`); schema changes are handled by `migrate()` on load — older state files are brought up to the current schema automatically, so existing files are safe to keep across updates.

Per-country, state holds: last 14 days of (new_resets, combat_ratio, resetter_combat_ratio) snapshots; cached citizen IDs (the "known veterans" cohort, refreshed weekly); per-citizen last-known reset timestamps (so a fresh reset on the next run can be detected); last urgent-alert timestamp and count (for cooldown logic).

Top-level: schema version, last run timestamp, and the list of country IDs flagged on the previous run (used to detect stand-downs).

Safe to delete; you'll just lose baselines and the first ~5 runs after deletion will be silent again.

## Notes

- Ireland's country ID is hardcoded (`IRELAND_COUNTRY_ID`); swap it to monitor a different home country.
- API access goes through the public proxy at `warera-proxy.toie.workers.dev/trpc`.
- 3 hops is a reasonable default for a country with Ireland's geography. Drop to 1–2 if the watchlist gets noisy; bump to 4+ if long-range threats via chained conquests are a real concern.
- After a schema migration, the first run will produce `new_resets = 0` for everyone — there's no prior reset cache to compare against, so all observations are first-time and seed the cache silently. From run 2 onwards the metric reflects real events between runs.