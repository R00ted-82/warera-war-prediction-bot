# warera-war-alert

Discord bot that watches War Era for countries gearing up for war against Ireland (and ones that are visibly standing down). Every three hours it samples each watchlisted country's most active high-level players for skill rebuilds and shifts in combat-vs-economy skill allocation, and posts a digest when something looks worth knowing, plus dedicated alerts for high-severity events. Once a day it posts a posture overview of how the whole watchlist splits between war and economy footing.

Sibling of [`warera-tools-ireland`](https://github.com/to-ie/warera-tools-ireland): same proxy, same Discord style, separate repo for focus.

## What you'll see

Every run posts at most one **update** embed listing the countries that escalated or de-escalated this run. Each country is read on two signals shown side by side: its actual war activity (wars declared/ended and weekly combat-damage rank) and its top ~50 active players' skill builds (combat vs economy — a leading indicator that lags real fighting). The embed leads with a short explainer so first-time readers aren't guessing.

Once a day, on the last scheduled run, the bot also posts a **posture overview** embed, a standing picture of the watchlist regardless of whether anything is flagged. See [Posture overview](#posture-overview).

In addition, certain events get their own dedicated alert message so they don't get lost in the digest.

### Severity icons

| Icon | Meaning |
|---|---|
| 🔴 | Active mobilisation or major combat shift, direct threat indicator |
| 🟠 | Significant mobilisation or shift, caution |
| 🟡 | Minor drift toward combat, keep an eye on it |
| 🟢 | Country is standing down or rebuilding toward economy, reassuring |
| 🕊️ | Dedicated stand-down alert, strong de-escalation signal |
| ✅ | Country no longer flagged since last run |
| 📊 | Daily posture overview, informational, not an alert |

Red is always danger to Ireland. Green is always reassurance. Yellow and orange sit on the mobilisation spectrum between them.

## What it detects

Every signal is reported as **one short line** in the per-run update (icon + country + a few words). A single country can trip several in one run; the update collapses each country to its single most important line. Damage-based signals are the accurate core; skill-build signals are a *leading* indicator that lags real fighting.

The primary signal is **weekly damage dealt** (`weeklyCountryDamages`, a rolling 7-day total with a game-wide rank). Each country is modelled as in one of two phases — *attacking* or *quiet* — with hysteresis between two thresholds (`DAMAGE_ACTIVE` / `DAMAGE_QUIET`) so it can't flap, and the transitions are what get reported:

**🔴 Started attacking.** Weekly damage crossed from quiet up into the attacking band — a country that wasn't fighting now is. The earliest concrete "war started" signal.

**🔴 Sustained offensive.** A country has stayed in the attacking band for `SUSTAINED_DAYS` (default 4) running — not a one-off skirmish but an ongoing campaign. Fired once per offensive.

**🟢 Went quiet.** Weekly damage fell back below the quiet threshold after a spell of attacking — the fighting has dropped off.

**🟠 Arming up / 🟢 Easing off.** The country's war-vs-economy skill build shifted significantly (combat focus up = arming, down = easing). Easing-off is suppressed while Ireland is actively at war with the country — a build dip isn't de-escalation when shells are still flying.

**🔴 Declared war / 🟢 War ended.** The country's own `warsWith` list is diffed run-to-run. A war declared *on Ireland* is the single highest-severity thing the bot reports; a war dropping off the list is de-escalation. Only diffed once a prior record exists, so an existing war list is never mistaken for a fresh declaration.

**🟠 Rebuilding for war / 🟢 Rebuilding for economy.** A cluster of players paying gold to reset their skills toward combat (or economy) — the earliest *leading* signal, before any damage shows. Skipped when the country is already saturated in that direction (an 88%-combat country gaining two more combat builds is routine reinforcement, not a fresh move).

### Holding and standing down

A country that was on the watchlist but produces no fresh signal this run is classified as either **standing down** (combat focus fell meaningfully from its flagged peak → a 🟢 "no longer flagged" line in the update) or **holding at high readiness** (still elevated, or Ireland is at war with it → stays on watch, shown with a `held` tag in the daily report, not re-alerted every run).

## Posture overview

Once a day, the 📊 overview shows the whole watchlist as **one unified roster, each country listed exactly once** — no country appears in two or three overlapping sections any more:

- **At-war callout.** Countries Ireland is at war with, each with its weekly-damage rank, listed first — war status trumps skill build.
- **Everyone else, most fighting first.** Every other watched country in one list sorted by weekly-damage rank (real fighting, most-active first). Each line shows the build as plain English (`mostly combat` / `leaning combat` / `mixed` / `mostly economy`), so a fighting-but-economy country reads naturally as "mostly economy · #11 in damage" instead of a jarring "0%". A country we couldn't skill-sample still appears here with its damage rank and "build unknown"; movers carry an inline 📈/📉 and a country still mobilised carries a "still on alert" tag — so nothing is a separate section and **the roster is never truncated**.
- **No data this run.** Footnote listing only countries with no readable data at all (no sample and no damage rank).
- **Daily heartbeat.** The report opens with a one-line status, "All quiet today" when nothing fired, or a brief count of the day's signals. Since it always posts daily, it doubles as confirmation the bot ran.

It's gated to one post per day on the first run at or after **20:00 UTC (8pm)**, tracked by date so a manual trigger can't double-post. The workflow has a dedicated `0 20 * * *` cron so the report fires at 8pm rather than waiting for the next 3-hourly slot.

## Example alerts

Every per-run signal is one short line (`icon · country · what happened`), bundled into a single compact **Update** embed — escalating lines first (🔴 high / 🟠 med / 🟡 low), then de-escalating (🟢). A country with several signals collapses to its single most important line.

> **🛡️ War Watch · Update**
> 🔺 5 getting more dangerous · 🔻 3 calming down
>
> 🔴 **Belgium** declared war on Ireland
> 🔴 **Germany** has been attacking hard for 6 days straight, #8 in the game for damage
> 🔴 **Morocco** started attacking — it was quiet, now it's fighting (now #42 in the game for damage)
> 🟠 **Denmark**'s players are shifting toward combat builds (35% → 62% on combat skills)
> 🟠 **Iceland** — 3 top players just retrained for combat
> 🟢 **Canada**'s players are shifting back toward economy (52% → 31% on combat skills)
> 🟢 **Norway** stopped attacking — its damage has dropped off
> 🟢 **Cuba** has calmed down — no longer a concern

The once-a-day posture report is the unified roster described above:

Every watched country is listed exactly once — at-war countries in their callout, everyone else in one list sorted by recent damage. A country we couldn't skill-sample still appears with its damage rank (build marked unknown); only a country with no data at all drops to a footnote.

> **📊 War Watch · Daily Posture Report**
> Today: **1** getting more dangerous, **3** still on alert.
> All **13** watched countries, sorted by how much fighting they've done lately (#1 = most damage in the game). *The build label is how their top players are skilled — a hint at intent that lags the real fighting.*
>
> **⚔️ At war with Ireland (2)**
> **Belgium** · #8 in damage · mostly economy
> **Netherlands** · #11 in damage · mostly economy
>
> **📊 Everyone else, most fighting first (11)**
> **Portugal** · #3 in damage · mostly combat · still on alert
> **Lithuania** · #6 in damage · mostly combat · still on alert
> **France** · #17 in damage · mostly combat
> **Spain** · #38 in damage · mostly combat
> **United States** · #46 in damage · mostly economy
> **Canada** · #84 in damage · mixed · 📉 easing to economy
> **Denmark** · #87 in damage · mostly economy
> **Morocco** · #95 in damage · mostly economy
> **Iceland** · #110 in damage · mostly economy
> **Cuba** · #120 in damage · build unknown (too few players)
>
> *(a country with no data at all shows under a small "No data this run" footnote)*

## Who it watches

The bot doesn't monitor every country in the game, only those that pose a credible threat. The watchlist rebuilds every run from two sources:

**Border controllers.** Countries controlling a region within `BORDER_HOPS` (default 3) hops of Ireland's territory. The walk is anchored on Ireland's fixed home region IDs (`IRELAND_REGION_IDS`) rather than whatever Ireland currently owns, so it keeps working while Ireland is **occupied**, when its home regions are held by an invader and would otherwise match nothing. Regions Ireland currently owns are folded into the anchor too, so territorial **expansions** through conquest are picked up automatically. The adjacency graph is walked live from `region.getRegionsObject` each run. The run log prints a `[debug]` line showing regions matched and an `OCCUPIED` note when Ireland holds no territory, so a silently-shrunk watchlist is visible.

**Diplomatic enemies.** Countries listed in Ireland's `warsWith` field, pulled fresh each run from `country.getCountryById`.

Typical watchlist size: 5-15 countries. Runs complete in 3-5 minutes.

## How it samples

For each watchlisted country, fetch lite profiles for the country's cached player IDs from prior runs. If the cache is stale (>7 days) or too many players have dropped below the activity threshold, paginate `user.getUsersByCountry` to rediscover the cohort. Newest-first, up to 15 pages.

A qualifying player is level 20+ and connected within the last 14 days. The top 50 by level make the sample (fewer for small countries, with `MIN_SAMPLE` as the floor below which a country is skipped). Note this is the highest-level **active players**, not players built for combat, so an economy-heavy country's sample is mostly workers, which is why the wording says "players" throughout.

Per sample, the bot tracks:

- **new_resets** — count of players whose `lastSkillsResetAt` advanced since the previous run. True event count, not windowed.
- **combat_ratio** — median combat-skill allocation across the sample.
- **resetter_combat_ratio** — median combat allocation of just those who rebuilt since last run.
- **combat_resets / eco_resets** — how many of those rebuilds ended up strongly combat- or economy-focused.

Combat skills: `attack`, `precision`, `dodge`, `armor`, `lootChance`, `criticalChance`, `criticalDamages`, `health`. Economy: `companies`, `entrepreneurship`, `production`, `management`. Energy and hunger are excluded as ambiguous.

## Detection thresholds

| Detector | Fires at |
|---|---|
| Reset burst (absolute floor) | 8+ rebuilds in one run, always |
| Reset burst (σ-based) | 2σ above outlier-filtered rolling baseline (5+ runs of history) |
| Burst direction | combat (rebuilders 70%+ combat) = war-prep; economy (0 combat rebuilds, 30% or less) = green; otherwise mixed/amber |
| Combat-intent rebuild | 2+ rebuilds with rebuilder combat 70%+, or 1 plus a +5pt 7-day shift |
| Ratio creep (yellow) | combat focus gained 20-40 points in 7d, or 30+ in 1d |
| Ratio creep (orange) | combat focus gained 40-60 points in 7d, or equivalent 1d |
| Ratio creep (red) | combat focus gained 60+ points in 7d, or equivalent 1d → dedicated alert |
| Ratio collapse (green) | mirror of creep on the falling side, 50+ points → dedicated alert |
| Eco-intent rebuild | 2+ rebuilds with rebuilder combat 30% or less, or 1 plus a -5pt 7-day shift |

Combat bursts at 1.5× their threshold OR 20+ absolute rebuilds trigger a dedicated "War Preparation Detected" alert. Red-tier ratio creep triggers a dedicated "Major Combat Shift" alert. 50+ point drops trigger "Standing Down" alerts.

All dedicated alerts have a per-country 3-day cooldown that's bypassed if severity escalates by 50%+.

### Outlier-filtered baselines

A country's own past mobilisations are excluded from its rolling baseline (any check where `new_resets` hits the absolute floor of 8 doesn't count toward the mean). Without this, a country that mobilised once becomes harder to detect next time. Falls back to the unfiltered series if filtering would leave too little baseline data.

### History length

The rolling history keeps `HISTORY_LEN = 56` entries per country. At the 3-hour cron cadence, that's ~7 days of context. To restore the previous ~14 days, set `HISTORY_LEN = 112`. If you change the cron cadence again, scale it accordingly (`days_of_context × runs_per_day`). The 7-day comparisons used by creep, collapse, and the posture movers need at least a week of history to fire, so don't drop `HISTORY_LEN` below one week's worth of runs.

## Setup

1. **Repo files.** `war_alert.py`, `requirements.txt` (just `requests`), `.github/workflows/war_alert.yml`, and this README.
2. **Discord webhook.** New channel for war alerts. Channel settings → Integrations → Webhooks → copy URL.
3. **GitHub secret.** Settings → Secrets and variables → Actions. Name `WAR_DISCORD_WEBHOOK_URL`, value = webhook URL.
4. **First run.** Actions → "War preparation alert" → Run workflow. Takes 3-5 minutes, then commits `war_state.json`.
5. **First day is calmer than later.** With the always-on absolute floor, you'll see real mobilisations from run 1, but the σ-based baseline detector still needs ~5 runs (~15 hours at 3h cadence) before it engages. False positives drop further after that.

## Tuning

Constants at the top of `war_alert.py`. The ones you'll most likely touch:

| Constant | Default | What it does |
|---|---|---|
| `IRELAND_REGION_IDS` | 4 IDs | Ireland's fixed home regions, the anchor for border detection. Update only if the game remaps Ireland |
| `BORDER_HOPS` | 3 | How far out from Ireland's territory to watch. 1 = direct borders only |
| `SAMPLE_TOP_N` | 50 | How many top players per country to sample |
| `NO_BASELINE_RESET_FLOOR` | 8 | Always-on absolute floor for burst alerts |
| `RESET_FLOOR` | 6 | σ-path floor once baseline exists |
| `HIGH_SEVERITY_FLOOR` | 20 | Absolute rebuilds for a dedicated burst alert |
| `RATIO_CREEP_MIN` | 20.0 | Minimum 7-day combat-focus gain for yellow creep |
| `RATIO_CREEP_RED` | 60.0 | 7-day gain that promotes to red + dedicated alert |
| `RATIO_DROP_MIN` | 20.0 | Minimum 7-day combat-focus drop for green standdown |
| `HIGH_DEMOB_FOR_ALERT` | 50.0 | Drop that triggers dedicated standdown alert |
| `COMBAT_INTENT` | 70.0 | Rebuilder combat % counted as "rebuilt for combat" |
| `DEMOB_RESET_INTENT` | 30.0 | Rebuilder combat % counted as "rebuilt for economy" |
| `STAND_DOWN_RATIO_CEILING` | 50.0 | Combat focus above this counts as still in posture, not stood down |
| `STAND_DOWN_DROP_MIN` | 15.0 | Drop from flagged peak needed to call a country stood down |
| `POSTURE_WAR` / `POSTURE_LEAN` | 70 / 50 | War-footing and combat-leaning thresholds in the posture overview |
| `POSTURE_MOVER_MIN` | 5.0 | Minimum 7-day shift to list a country as a mover |
| `MIN_LEVEL` | 20 | Minimum player level to include in sampling |
| `URGENT_COOLDOWN_DAYS` | 3 | Per-country cooldown between repeat urgent alerts |
| `HISTORY_LEN` | 56 | Rolling history entries per country (~7 days at 3h cadence) |

If the larger 50-player sample plus the higher reset floors make alerts feel too quiet, dialling `RESET_FLOOR` and `NO_BASELINE_RESET_FLOOR` down a couple of points is the main lever.

## State

`war_state.json` lives in the repo and is auto-committed every run. Holds per-country aggregate snapshots with rolling history, cached player IDs for fast next-run sampling, cooldown timestamps for each alert type, the peak combat ratio recorded while a country was flagged (for the stand-down vs holding decision), and the date of the last posture overview. Stays well under 1MB.

The state file is migrated automatically on first run after a schema change. Current version is v9, which added per-country damage-phase tracking (`damage_phase`, `damage_phase_since`, `sustained_reported`) on top of v8's military-activity fields (`wars_with`, `weekly_damage`, `weekly_damage_rank`, `total_damage_rank`, `active_pop`). Migrations are idempotent.

Delete `war_state.json` to reset baselines. The first ~5 runs after that will rely on the absolute floor only. Note that when a country re-enters the watchlist after being absent (for example after an occupation ends, or after the watchlist was broken), its first run re-counts every rebuild since it was last sampled as "new," which can briefly look noisy until the next run refreshes its baseline.

## Schedule

Runs every 3 hours via GitHub Actions cron (`0 */3 * * *`), at 00:00, 03:00, … 21:00 UTC, plus a dedicated `0 20 * * *` run at 20:00. At this cadence the worst-case lag between an event and an alert is ~3 hours and the average is ~1.5 hours. The posture overview is gated to the first run at or after 20:00 UTC (8pm) so it lands once a day, at 8pm.

The workflow has a concurrency group, so two runs can't race on the state file.

### Heartbeat and missed runs

Two things confirm the bot is alive. The daily posture report opens with an "all quiet" line on calm days, so a healthy run is visible once a day even when nothing fires. And a staleness watchdog at the start of each run compares against the previous `last_run`: if it's older than `STALE_RUN_HOURS` (default 9, about three missed 3-hour runs), it posts a degraded-health alert and then proceeds normally. Lower `STALE_RUN_HOURS` toward 6-7 to catch a single missed run. The watchdog can only fire on a run that actually executes, so it flags a gap once runs resume rather than the instant the scheduler dies; true dead-man coverage would need an external pinger.

## Known gaps

**State-owned ammo production** isn't detected. Countries that stockpile through their own companies without player retraining will fly under the radar. Would need a separate watcher on `transaction.getPaginatedTransactions`.

**Impulsive wars without a prep window** are now caught at declaration time via the `warsWith` diff (a war declared on Ireland is the highest-severity signal the bot raises), and the fighting that follows surfaces as a "started attacking" / "sustained offensive" damage-phase flag. To catch individual attacks-in-progress at finer grain, watch battle creation events directly.

**Pre-mobilisation diplomacy** isn't observable. By the time the first player pays gold to rebuild, the political decision was made days or weeks earlier. The earliest signal in the game data is the first rebuild wave.

**Tiny countries** with fewer than 10 active level-20+ players can't be skill-sampled and show as "couldn't skill-sample" in the posture report — but their weekly-damage rank is still fetched and shown there, so an unreadable country that's actually fighting hard no longer disappears entirely.

## Credits

Data via the [warera-proxy](https://warera-proxy.toie.workers.dev/) gateway and [warerastats.io](https://warerastats.io/). Made by toie.