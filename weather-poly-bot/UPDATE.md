# Weather Bot — Update v8 (HRRR / regional models + METAR floor + OpenWeather)

## What's new

### 🛰️ Regional high-res models, auto-selected per location

Each Polymarket city now uses the best regional NWP model available:

| Region | Model | Resolution | Coverage |
|---|---|---|---|
| US (NYC, LA, Chicago, etc.) | NOAA HRRR | 3km | CONUS, 0–18h, hourly updates |
| Canada (Toronto) | EC HRDPS | 2.5km | Canada, 0–48h |
| UK (London) | UKMO 2km | 2km | UK + Ireland, 0–48h |
| France (Paris) | Météo-France AROME HD | 1.5km | France + neighbors, 0–42h |
| Wider Europe (Madrid, Warsaw, Helsinki, Moscow, Ankara, Tel Aviv) | ARPEGE Europe | 11km | Europe, 0–114h |
| Japan (Tokyo) | JMA MSM | 5km | Japan, 0–78h |
| Asia / SA / Africa / Oceania | _falls back to global ensemble_ | | |

When a regional model returns data, it gets **30%** of the blend weight,
with the rest of the weight redistributed proportionally among the global
models (ECMWF IFS still leads at 16-17%).

When no regional applies, the existing 8-model global ensemble runs as
before, with ECMWF IFS slightly bumped to 25% (was 22%).

### 📡 METAR observed-max floor for today

For today's forecast specifically: we pull METAR observations from the past
12 hours at the airport. The max observed temperature so far is used as a
**hard floor** on the predicted max — because you can't have a daily max
less than what already happened. If the floor displaces the model's
prediction, uncertainty is bumped slightly (the forecast under-shot).

This is the "absolute truth" anchor you asked for: METAR observations
trump model predictions when they conflict.

### 🌐 OpenWeather added (optional)

When `OPENWEATHER_API_KEY` is set in `.env`, OpenWeather's daily-max forecast
joins the ensemble with a small **5%** weight as a cross-source check. If
not set, it's silently skipped — no behavior change.

---

## Files in this zip

| File | Status | Notes |
|---|---|---|
| `weather.py` | Major changes | Regional model selection, METAR floor, OpenWeather, weight rebalancing |
| `bot.py` | Small changes | Pass `icao` to `fetch_ensemble_forecast` at all 5 call sites |
| `env.example` | Renamed from `.env.example` to be visible | Adds `OPENWEATHER_API_KEY` config |

No new Python dependencies. No DB migration.

---

## DEPLOY — STEP BY STEP

### Part A — On your laptop

**1. Unzip** `weather-bot-update-v8.zip`. You get `bot.py`, `weather.py`,
   `env.example`, and `UPDATE.md`.

**2. Open your local weather-bot repo folder** on your computer.

**3. Drag `bot.py` and `weather.py`** into your repo folder, replacing the
   old versions. Don't drag `env.example` or `UPDATE.md`.

**4. Open your weather-bot repo on GitHub** in the browser.

**5. Add file → Upload files.** Drag `bot.py` and `weather.py` into the
   upload area. GitHub will note both files are being replaced — that's
   correct.

**6. Commit message:**
```
v8: HRRR/regional models + METAR floor + OpenWeather (optional)
```
Click **Commit changes**.

### Part B — On the VPS in PuTTY

**7. Pull the new code:**
```bash
cd ~/weather-bot
git pull
```

**8. (Optional) Add your OpenWeather API key.** This step is only if you
want OpenWeather added to the blend. Skip if you're happy without it.

  a. Get a free API key at https://openweathermap.org/api → "One Call API 3.0"
     (the free tier is 1,000 calls/day, way more than the bot will use).

  b. Edit your `.env` file in PuTTY:
```bash
nano ~/weather-bot/.env
```

  c. Find the line `LOG_LEVEL=INFO` and below it add a new line:
```
OPENWEATHER_API_KEY=your_key_here
```
   (Paste your actual key in place of `your_key_here`. No spaces, no quotes.)

  d. Save: `Ctrl+O`, press Enter, then `Ctrl+X` to exit.

**9. Restart the bot:**
```bash
sudo systemctl restart weather-bot
sleep 3
sudo systemctl status weather-bot --no-pager
```

You want `Active: active (running)`. Press `q` to exit.

### Part C — Verify

**10. Watch logs while testing:**
```bash
sudo journalctl -u weather-bot -f
```

**11. In Telegram, run `/forecast KJFK`.** In the live logs you should see:
- `metar floor: ... raised ...` (only if today's high already happened — i.e. it's afternoon)
- No errors

**12. Check that regional models are firing.** Run `/forecast EGLL` (London).
The bot's response is the same format as before — but if you have
`/forecast KJFK` showing a slightly different prediction than before, that's
HRRR doing its job. (You won't see HRRR explicitly named in the user-facing
text; if you want me to add a "Sources used" footer line, let me know.)

**13. Press Ctrl+C** to exit logs (bot keeps running).

---

## Troubleshooting

**Bot fails to start after pull:**
```bash
sudo journalctl -u weather-bot -n 30 --no-pager
```
Paste the last 20 lines if anything's wrong.

**OpenWeather API key invalid:**
The bot silently skips OpenWeather if the key is wrong or absent. Check the
log for `openweather fetch failed:` lines. Get a new key at
openweathermap.org if needed.

**Predictions seem unchanged after deploy:**
Regional models give the biggest boost in the 0–18h horizon (today's max).
For tomorrow + day after, the global ensemble dominates anyway, so the
prediction won't shift dramatically. METAR floor only triggers when the
day's high has already passed and the model under-shot it.

**Want to confirm METAR floor is working?**
Test on a city in the late afternoon / early evening (after the daily high
typically occurs). The log will show `metar floor: X.X°C raised ...` if it
displaced the model.
