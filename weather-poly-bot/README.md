# 🌦️ WeatherPolyBot

> **Precision temperature forecasting + Polymarket edge detection, delivered via Telegram.**

A Telegram bot that blends three elite NWP (Numerical Weather Prediction) models into a single best-guess daily high temperature, then scans Polymarket for profitable edges — all running 24/7 on your VPS even when PuTTY is closed.

---

## 🧠 How It Works

### Model Selection & Rationale

Three models were chosen based on peer-reviewed accuracy studies and WMO verification data:

| Model | Source | Global MAE (500hPa) | Temp Accuracy (±2°C) | Why Chosen |
|-------|--------|--------------------|-----------------------|------------|
| **ECMWF IFS** | European Centre | Lowest globally | ~92% | Ranked #1 by WMO Lead Centre 2015–2024 |
| **NOAA GFS** | NOAA / NCEP | 2nd–3rd globally | ~88% | Best for N. America; 4× daily updates |
| **DWD ICON** | German Weather Service | Consistently 3rd | ~89% | Independent icosahedral core; true model diversity |

*Sources: WMO Lead Centre for Deterministic NWP, Raynaud & Bouttier (2017), ECMWF Forecast Performance 2023 Report*

### Ensemble Weighting

Weights are **not fixed** — they are derived from a 30-day backtest across 12 diverse locations (airports + cities on 6 continents). After each backtest:

```
w_i = exp(-MAE_i) / Σ exp(-MAE_j)
```

Lower MAE → exponentially higher weight. Weights are persisted to SQLite and reloaded on startup.

### Edge Detection

For each Polymarket temperature market:
1. Extract the temperature threshold from the question text
2. Compute `P_model(actual_high > threshold)` using our predicted high and a ±1.5°C Gaussian uncertainty
3. Compare to the current YES price: `edge = P_model − market_price`
4. Flag any edge ≥ **40¢/share** as a trade opportunity

---

## 📁 Project Structure

```
weather-poly-bot/
├── bot.py                  # Main Telegram bot, all handlers
├── weather_service.py      # Multi-model fetch + ensemble prediction
├── backtest_service.py     # 30-day backtesting + weight calibration
├── polymarket_service.py   # Market search, price fetch, edge calc
├── database.py             # Async SQLite (aiosqlite)
├── keyboards.py            # All InlineKeyboardMarkup layouts
├── config.py               # All constants and settings
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variable template
├── weatherpolybot.service  # systemd unit file
├── deploy.sh               # One-shot deploy script
└── update.sh               # Hot-update script (no downtime)
```

---

## 🚀 Deployment: Step-by-Step

### Prerequisites

- Ubuntu 22.04 / Debian 12 VPS (any cloud: DigitalOcean, Linode, Hetzner, etc.)
- Root or sudo access
- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

---

### Step 1 — Get Your Telegram Bot Token

1. Open Telegram → search `@BotFather`
2. Send `/newbot`
3. Choose a name (e.g. `My Weather Bot`)
4. Choose a username ending in `bot` (e.g. `myweatherforecast_bot`)
5. Copy the token — it looks like: `7123456789:AAHdqTcv...`

---

### Step 2 — Upload to GitHub

```bash
# On your local machine
cd weather-poly-bot
git init
git add .
git commit -m "Initial commit"
git branch -M main

# Create a repo on GitHub, then:
git remote add origin https://github.com/YOUR_USERNAME/weather-poly-bot.git
git push -u origin main
```

---

### Step 3 — Deploy on VPS (one command)

SSH into your VPS with PuTTY (or any SSH client):

```bash
ssh root@YOUR_VPS_IP
```

Edit `deploy.sh` first to set your GitHub repo URL, then run:

```bash
# Download and run the deploy script directly
curl -fsSL https://raw.githubusercontent.com/YOUR_USERNAME/weather-poly-bot/main/deploy.sh | sudo bash
```

**Or** clone manually then deploy:

```bash
git clone https://github.com/YOUR_USERNAME/weather-poly-bot.git /tmp/wpb
sudo bash /tmp/wpb/deploy.sh
```

---

### Step 4 — Set Your Bot Token

```bash
sudo nano /opt/weather-poly-bot/.env
```

Change:
```
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
```
→
```
TELEGRAM_BOT_TOKEN=7123456789:AAHdqTcvXXXXXX
```

Save: `Ctrl+O` → Enter → `Ctrl+X`

---

### Step 5 — Start the Bot

```bash
sudo systemctl start weatherpolybot
sudo systemctl status weatherpolybot
```

You should see `active (running)`. **Close PuTTY — the bot keeps running.**

---

### Step 6 — Open Telegram and Test

1. Search for your bot username in Telegram
2. Send `/start`
3. The main menu will appear with all buttons

---

## 🔧 Management Commands

```bash
# View live logs
sudo journalctl -u weatherpolybot -f

# Restart bot
sudo systemctl restart weatherpolybot

# Stop bot
sudo systemctl stop weatherpolybot

# Update to latest code from GitHub
sudo bash /opt/weather-poly-bot/update.sh

# Check if bot auto-starts on reboot
sudo systemctl is-enabled weatherpolybot
```

---

## 🤖 Bot Features

| Feature | Description |
|---------|-------------|
| 🌡️ **Predict** | Today + tomorrow high in °C and °F, per-model breakdown |
| 📡 **Track Location** | Auto-refresh any location every 60 min |
| 🎯 **Market Scanner** | Scans Polymarket, flags edges ≥ 40¢/share |
| 💼 **Positions** | Log shares, entry price, get live P&L |
| ⭐ **Favourites** | Pin markets for instant price refresh |
| 📊 **Backtest** | View 30-day model accuracy results |
| 🔄 **Re-run Backtest** | Re-calibrate model weights on demand |

---

## 🌍 Backtest Locations

The 30-day backtest runs across these 12 locations automatically on first launch:

| Location | Type |
|----------|------|
| JFK Airport, New York | Airport |
| LAX, Los Angeles | Airport |
| London Heathrow (LHR) | Airport |
| Tokyo Haneda (HND) | Airport |
| Sydney Airport (SYD) | Airport |
| Dubai Airport (DXB) | Airport |
| Chicago O'Hare (ORD) | Airport |
| Paris CDG | Airport |
| Toronto Pearson (YYZ) | Airport |
| Miami International | Airport |
| Denver International | Airport |
| São Paulo Guarulhos (GRU) | Airport |

---

## ⚙️ Configuration (config.py)

| Setting | Default | Description |
|---------|---------|-------------|
| `EDGE_THRESHOLD` | `0.40` | Minimum edge ($/share) to flag a trade |
| `PRED_UNCERTAINTY_C` | `1.5` | Gaussian σ for probability estimation |
| `TRACKED_REFRESH_MIN` | `60` | Minutes between auto-refresh |
| `BACKTEST_DAYS` | `30` | Days of history in backtest |

---

## 📡 Data Sources

| Source | What For | Cost |
|--------|----------|------|
| [Open-Meteo Forecast API](https://open-meteo.com) | Live model forecasts | Free |
| [Open-Meteo Historical Forecast API](https://open-meteo.com) | Backtest: model hindcasts | Free |
| [Open-Meteo Archive API](https://open-meteo.com) | Backtest: observed temps | Free |
| [Open-Meteo Geocoding API](https://open-meteo.com) | Location search | Free |
| [Polymarket Gamma API](https://gamma-api.polymarket.com) | Market prices & search | Free |

**No API keys required** — everything runs on free public endpoints.

---

## 🛡️ Security Notes

- The bot only responds to Telegram messages (no open ports required)
- `.env` file is `chmod 640` (readable only by `weatherbot` user and root)
- systemd hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`
- The `weatherbot` system user has no shell and cannot log in

---

## 🐛 Troubleshooting

**Bot not responding:**
```bash
sudo journalctl -u weatherpolybot -n 50
```

**"Token not set" error:**
```bash
sudo nano /opt/weather-poly-bot/.env
sudo systemctl restart weatherpolybot
```

**Backtest fails / no data:**
Open-Meteo archive has a ~2-day lag. This is normal and handled automatically.

**Permission denied on .env:**
```bash
sudo chown weatherbot:weatherbot /opt/weather-poly-bot/.env
```

---

## 📄 License

MIT — do whatever you like with it.
