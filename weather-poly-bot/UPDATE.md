# Weather Bot — Update v9 (Position-level exit-signal alerts)

## What's new

A new alert type: **exit signals for tracked Polymarket positions**. Tells you
when a position you've taken is likely to lose, so you can close it out at a
better price than waiting for it to resolve.

### How it works
1. Find a bucket via 🎲 Polymarket or 🎯 Opportunities
2. Tap the new **📌 Track** button next to any bucket
3. Bot scans every 15 min during 12pm–5pm city local
4. When P(miss) ≥ 50%, you get a Telegram alert with:
   - Current METAR temp + trend (rising/falling/steady)
   - Max observed so far today
   - HRRR's projected rest-of-day max
   - Blended projection vs your bucket
   - "Already exceeded — cannot recover" if applicable
5. Re-alerts hourly while still in danger zone (until you /untrack_position)

### How P(miss) is computed
- METAR observations = ground truth for what already happened
- HRRR (or regional equivalent) projects remaining hours of the day
- Global ensemble as sanity check
- The blended projection's Gaussian gives P(actual_max < bucket_low) +
  P(actual_max > bucket_high) = P(miss)

### New commands
- `/track_position` — see your tracked positions
- `/untrack_position <id>` — stop tracking a specific position

### New buttons
- 📌 Track button on each bucket in opportunity-detail view
- 🔕 Untrack button on each alert message

## Files

| File | Status |
|---|---|
| `tracking.py` | New `positions` table (auto-migrates) + position add/remove/list |
| `weather.py` | New `compute_position_risk()` + METAR trend + HRRR remaining-day max |
| `bot.py` | New `/track_position`, `/untrack_position`, scanner job, alert formatter, callback handlers |

No new Python dependencies. Database auto-migrates on first run.

---

## DEPLOY — STEP BY STEP

### Part A — Laptop

**1.** Unzip `weather-bot-update-v9.zip`. You get `bot.py`, `weather.py`,
   `tracking.py`, and `UPDATE.md`.

**2.** Open your local weather-bot repo folder.

**3.** Drag `bot.py`, `weather.py`, and `tracking.py` into your repo folder,
   replacing the existing versions. Don't drag `UPDATE.md`.

**4.** Open your weather-bot repo on GitHub in the browser.

**5.** Add file → Upload files → drag the three .py files. GitHub will say
   they're being replaced — that's correct.

**6.** Commit message:
```
v9: Position-level exit-signal alerts (METAR + HRRR + ensemble)
```
Click **Commit changes**.

### Part B — VPS in PuTTY

```bash
cd ~/weather-bot
git pull
sudo systemctl restart weather-bot
sleep 3
sudo systemctl status weather-bot --no-pager
```

You want `Active: active (running)`. Press `q` to exit.

### Part C — Verify

**Watch logs while testing:**
```bash
sudo journalctl -u weather-bot -f
```

You should see startup lines including:
```
Tracking job scheduled every 30 min
Position scan job scheduled every 15 min
Loaded NN airports
Starting bot — long polling
```

Press Ctrl+C when done (bot keeps running).

**In Telegram:**
1. `/start` — should now show 📌 commands in the bot menu
2. Tap **🎯 Opportunities** → tap any opportunity → **Details**
3. You should see new **📌 Track** buttons next to each bucket's Trade button
4. Tap **📌 Track** on a bucket → bot confirms the position is tracked
5. `/track_position` → see your list with each position's ID
6. To remove: `/untrack_position 1` (or use the 🔕 button on alert messages)

The exit-signal alert will fire when:
- Today is the position's target date
- City local time is between 12pm and 5pm
- P(miss) reaches ≥50%
- It's been ≥1h since the last alert for that position

So if you track an NYC position right now and it's currently 1pm ET, you
might get an alert within 15 minutes if conditions warrant. Outside the
12-5pm window, no alerts fire (forecast risk is mostly settled by then or
hasn't started building).

---

## Troubleshooting

**Bot fails to restart:**
```bash
sudo journalctl -u weather-bot -n 30 --no-pager
```
Paste the last 20 lines if anything looks wrong.

**Tracked position but no alerts:**
Check whether you're inside any city's 12pm–5pm window:
```bash
sudo journalctl -u weather-bot -f | grep position_scan
```
You should see scan attempts every 15 min. Lines like
`position_scan: alert sent for #N` mean an alert fired. Lack of alert
during the window means P(miss) is below 50% — your position is still
healthy.

**Want to test the alert formatting without waiting for real conditions:**
Let me know — I can add a `/debug_alert <position_id>` command that fires
a test alert immediately on demand.
