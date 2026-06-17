---
name: skill-ratio-vs-weekly-damage
description: Why the skill-combat-focus metric mislabels real military powers, and the accurate signal that replaces it
metadata:
  type: project
---

In warera-war-prediction-bot, the original "combat focus" metric (median combat-vs-economy skill-point share among the top-50-by-level active players) is **anti-correlated with actual military activity** for the heavyweight countries. Measured 2026-06-15 from live data: Netherlands read 0% combat focus while ranked **#5 in the game for weekly damage** (#2 all-time, at war with 11); Germany 27% while #9 weekly / **#1 all-time**; Belgium 8% while #4 all-time. Meanwhile France/Lithuania/Spain read 81-88% "war-ready" but ranked only #16-#35 weekly. Reason: the real powers' elite players invest skills into companies/production to *fund* the war machine and fight through gear/rank, so skill focus is a leading indicator that lags (and inverts vs) actual fighting.

The accurate, cheap signal lives in `country.getCountryById` → `rankings.weeklyCountryDamages` (rolling 7-day combat damage, with a game-wide rank), plus `warsWith`, `nonAggressionUntil`, `countryActivePopulation`. One call per watchlist country (~13-20), no per-player fan-out — far cheaper than the skill-sampling pipeline.

**How to apply:** treat weekly-damage rank + war-list diffs as the primary risk/de-escalation signal; keep skill rebuilds only as an early-warning supplement. Never present skill-combat % as a country's military strength without the damage rank beside it (the daily report shows build as a plain word — combat/mixed/economy — next to the rank, never a bald "0%"). Pulling the whole population (~6,634 citizens across the watchlist) would make accuracy *worse* (population median collapses toward 0%) and ~7x the API load — don't.

Notifications are deliberately terse (bunker-bot style): the per-run **Update** is one compact embed, one line per country (`icon · country · what happened`), escalating first then de-escalating. The daily **posture report** is one unified roster (each country once, sorted by weekly-damage rank), gated to fire once a day at 20:00 UTC (8pm; dedicated `0 20 * * *` cron). Signals: started_attacking / sustained_offensive / went_quiet (damage-phase state machine with hysteresis between DAMAGE_ACTIVE=3M and DAMAGE_QUIET=1M, SUSTAINED_DAYS=4), arming_up/easing_off (build shift), war_declared/war_ended, rebuild_war/rebuild_eco (skipped when already saturated). The old paragraph-style dedicated alerts (War Preparation Detected / Standing Down / Major Combat Shift) were removed. State schema is v9 (`damage_phase`, `damage_phase_since`, `sustained_reported` on top of v8's military fields). See [[warera-bot-overview]].
