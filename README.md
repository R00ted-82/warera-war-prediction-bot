# War Watch

Spots countries getting ready to attack Ireland by watching how their high-level players spend their skill points. War Era doesn't surface enemy preparation directly — but when citizens of a country start resetting their skills en masse, or when their combat allocation climbs day after day, it's a strong signal that mobilisation is underway.

Sibling of `alert.py` (production-bonus market notifier).

## What it watches

A dynamic watchlist of countries within strategic reach of Ireland, plus anyone Ireland's currently at war with:

- **Neighbours, up to 3 hops out.** Walks the game's region adjacency graph (`region.neighbors`) breadth-first from Ireland's territory. A country joins the watchlist if it controls any region within 3 hops. Direct attackers turn up at hop 1; countries that could plausibly project force via one or two intermediate conquests turn up at hops 2–3.
- **Active war opponents.** Anything in Ireland's `warsWith` list, regardless of distance.

Watchlist is rebuilt from live API data each run. No code change needed when territory shifts or wars start/end.

## Signals

For each watchlisted country the script samples up to 25 active high-level citizens (level ≥ 20, online in the last 14 days) and aggregates two numbers:

- **Resets in the last 5 days.** Skill resets cost gold, so people don't do them casually. A burst across the high-level cohort means citizens are repurposing themselves — almost always for combat.
- **Combat / economy skill ratio.** Of points spent on bucketed combat skills (attack, precision, dodge, armour, loot, crits, health) vs economy skills (companies, entrepreneurship, production, management), what fraction is combat.

Both go into a 14-day rolling history per country. From there:

- **Reset burst** — current 5-day count is ≥ 2σ above that country's own rolling baseline, or ≥ 5 absolute (whichever is higher).
- **Ratio creep** — combat ratio has climbed ≥ 20 percentage points since ~7 days ago.

First ~5 runs after deployment collect data silently while baselines build up.

## Output

Two kinds of Discord messages:

- **High-severity bursts** get their own embed: ≥ 1.5× the threshold or ≥ 10 absolute resets. Includes sparkline trends for reset count and combat ratio.
- **Everything else** rolls into a single daily digest with 🔴/🟠/🟡 severity icons.

If nothing's flagged, nothing's posted.

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
| `BORDER_HOPS` | 3 | How far out to expand the watchlist from Ireland |
| `SAMPLE_TOP_N` | 25 | High-level citizens sampled per country |
| `MIN_LEVEL` | 20 | Below this is too noisy to read combat intent |
| `ACTIVITY_WINDOW_DAYS` | 14 | "Active" if connected within this many days |
| `DISCOVERY_INTERVAL_DAYS` | 7 | How often to re-paginate citizen lists |
| `RESET_WINDOW_DAYS` | 5 | Reset lookback window |
| `BASELINE_SIGMA` | 2.0 | Burst sensitivity |
| `RESET_FLOOR` | 5 | Hard minimum reset count for any alert |
| `RATIO_CREEP_PP` | 20.0 | pp gain that triggers a creep alert |
| `RATIO_LOOKBACK_DAYS` | 7 | Creep lookback |

## State

`war_state.json` next to the script. Stores per country: last 14 days of snapshots, cached citizen IDs (the "known veterans" cohort, refreshed weekly), and last-run timestamp. Safe to delete; you'll just lose baselines and the first ~5 runs after deletion will be silent again.

## Notes

- Ireland's country ID is hardcoded (`IRELAND_COUNTRY_ID`); swap it to monitor a different home country.
- API access goes through the public proxy at `warera-proxy.toie.workers.dev/trpc`.
- 3 hops is a reasonable default for a country with Ireland's geography. Drop to 1–2 if the watchlist gets noisy; bump to 4+ if long-range threats via chained conquests are a real concern.