# 💰 RRL-Wealth Trading Bot

RRL-Wealth adalah bot trading otomatis untuk Binance USD-M Futures menggunakan strategi **RSI + EMA Confluence**. Dibangun dengan Python murni tanpa library third-party Binance — langsung hit REST API resmi.

---

## ✨ Fitur

- 📈 **Strategi RSI + EMA Confluence** — EMA 20/50 untuk trend, RSI 14 untuk timing entry
- 🔁 **Multi-Pair** — pantau banyak pair sekaligus (BTC, ETH, SOL, dll)
- 📊 **Backtest Engine** — simulasi 90 hari data historis sebelum live
- 📲 **Telegram Notifikasi** — alert real-time setiap buka/tutup posisi
- 🛡️ **Risk Management** — SL otomatis berbasis ATR, TP dengan RR 1:2, risiko 1%/trade
- 🧪 **Testnet Support** — switch testnet/live cukup ubah satu baris di `.env`

---

## ⚙️ Cara Kerja Strategi

| Kondisi | LONG | SHORT |
|--------|------|-------|
| Trend | Close > EMA50, EMA20 > EMA50 | Close < EMA50, EMA20 < EMA50 |
| Momentum | RSI < 35 dan mulai naik | RSI > 65 dan mulai turun |
| Stop Loss | Entry − ATR × 1.5 | Entry + ATR × 1.5 |
| Take Profit | SL distance × 2 (RR 1:2) | SL distance × 2 (RR 1:2) |

---

## 🚀 Quick Start

### 1. Clone repo

```bash
git clone https://github.com/USERNAME_KAMU/futures-bot.git
cd futures-bot
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Buat file `.env`

```bash
cp .env.example .env
nano .env   # isi API key kamu
```

Isi `.env`:

```env
BINANCE_API_KEY=isi_api_key_kamu
BINANCE_API_SECRET=isi_api_secret_kamu
TESTNET=true

# Opsional — notifikasi Telegram
TELEGRAM_TOKEN=isi_token_bot_telegram
TELEGRAM_CHAT_ID=isi_chat_id_kamu
```

### 4. Backtest dulu (wajib!)

```bash
# Backtest BTC 90 hari
python bot.py --backtest

# Backtest multi-pair 60 hari
python bot.py --backtest --pairs BTCUSDT ETHUSDT SOLUSDT --days 60
```

### 5. Jalankan bot

```bash
# Testnet (aman, uang palsu)
python bot.py

# Multi-pair
python bot.py --pairs BTCUSDT ETHUSDT SOLUSDT

# Live trading (pastikan TESTNET=false di .env)
python bot.py
```

---

## 📁 Struktur File

```
futures-bot/
├── bot.py              # Bot utama
├── requirements.txt    # Dependencies
├── .env.example        # Template konfigurasi
├── .gitignore          # Exclude .env & log
└── README.md           # Dokumentasi ini
```

---

## 🔑 Cara Dapat API Key

**Testnet (gratis, uang palsu):**
1. Buka [testnet.binancefuture.com](https://testnet.binancefuture.com)
2. Login dengan akun GitHub
3. Klik **API Key → Generate HMAC_SHA256 Key**
4. Copy ke `.env`

**Live trading:**
1. Buka [Binance → Profile → API Management](https://www.binance.com/id/my/settings/api-management)
2. Create API → pilih **System generated**
3. Enable **Futures trading** di permissions
4. Copy ke `.env` dan set `TESTNET=false`

---

## ⚙️ Konfigurasi Parameter

Edit bagian ini di `bot.py` sesuai kebutuhan:

```python
# Trading
DEFAULT_PAIRS   = ["BTCUSDT"]   # pair yang dipantau
LEVERAGE        = 5             # leverage (hati-hati!)
RISK_PERCENT    = 1.0           # % balance per trade
RR_RATIO        = 2.0           # risk:reward ratio
ATR_SL_MULT     = 1.5           # multiplier ATR untuk stop loss
MAX_OPEN_TRADES = 3             # maks posisi terbuka bersamaan

# Indikator
EMA_FAST        = 20
EMA_SLOW        = 50
RSI_PERIOD      = 14
RSI_OVERSOLD    = 35
RSI_OVERBOUGHT  = 65
```

---

## 📲 Setup Telegram Notifikasi

1. Cari **@BotFather** di Telegram → `/newbot` → ikuti instruksi → copy **TOKEN**
2. Cari **@userinfobot** di Telegram → `/start` → catat angka **Id**
3. Isi `TELEGRAM_TOKEN` dan `TELEGRAM_CHAT_ID` di `.env`
4. Kirim `/start` ke bot Telegram kamu terlebih dahulu

---

## 🖥️ Menjalankan di GitHub Codespace

Disarankan pakai **tmux** agar bot tetap jalan meski browser ditutup:

```bash
# Buat session
tmux new -s bot

# Jalankan bot
python bot.py

# Keluar tanpa matikan bot: Ctrl+B lalu D

# Kembali ke session
tmux attach -t bot
```

> ⚠️ Codespace free tier: **120 jam/bulan**. Upgrade ke GitHub Pro untuk 180 jam.

---

## 📊 Kriteria Backtest Layak Live

| Metrik | Minimal | Bagus |
|--------|---------|-------|
| Win Rate | ≥ 45% | ≥ 55% |
| Profit Factor | ≥ 1.3x | ≥ 1.8x |
| Max Drawdown | ≤ 20% | ≤ 10% |
| Net Return | ≥ +5% | ≥ +15% |

---

## ⚠️ Disclaimer

> Trading futures mengandung risiko tinggi. Bot ini dibuat untuk tujuan edukasi. Selalu gunakan risk management yang ketat, test di testnet terlebih dahulu, dan jangan pernah trading dengan uang yang tidak siap kamu kehilangan. Developer tidak bertanggung jawab atas kerugian yang terjadi.

---

## 📄 License

MIT License — bebas digunakan dan dimodifikasi.
