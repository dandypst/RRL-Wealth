"""
╔══════════════════════════════════════════════════════════════╗
║              RRL-WEALTH TRADING BOT v2.0                     ║
║       Strategi : RSI + EMA Confluence (Adaptive)             ║
║       Fitur    : Lessons System · Threshold Evolution        ║
║                  Dual Agent Loop · Telegram Notif            ║
║       API      : Binance USD-M Futures (Official REST API)   ║
╚══════════════════════════════════════════════════════════════╝

FITUR BARU v2.0:
  [1] Lessons System   - bot belajar dari setiap trade yang tutup.
                         Pola kondisi entry (RSI, ATR, jam, hari, EMA spread)
                         yang sering WIN vs LOSS dicatat ke lessons.json.
                         Dipakai sebagai filter sinyal berikutnya.

  [2] Threshold Evolve - setelah MIN_TRADES_TO_EVOLVE trade tutup,
                         bot auto-adjust RSI, ATR, RR berdasarkan performa.
                         Jalankan manual: python bot.py --evolve
                         atau otomatis setiap kelipatan 10 trade.

  [3] Dual Agent Loop  - dua thread berjalan paralel:
                         Scanner  (60s) : cari sinyal entry baru
                         Manager  (15s) : pantau posisi + trailing SL

BASE URL:
    Live    : https://fapi.binance.com
    Testnet : https://demo-fapi.binance.com

DEPENDENCIES:
    pip install pandas ta python-dotenv requests

FILE .env:
    BINANCE_API_KEY=xxx
    BINANCE_API_SECRET=xxx
    TESTNET=true
    TELEGRAM_TOKEN=xxx
    TELEGRAM_CHAT_ID=xxx

CARA PAKAI:
    python bot.py                 -> live bot (dual agent)
    python bot.py --backtest      -> backtest 90 hari
    python bot.py --evolve        -> trigger threshold evolution manual
    python bot.py --lessons       -> tampilkan lessons tersimpan
    python bot.py --pairs BTC ETH -> multi-pair
"""

import os, sys, json, time, hmac, hashlib, logging, argparse, threading, requests
from datetime import datetime
from urllib.parse import urlencode
from dotenv import load_dotenv
import pandas as pd
import ta

# ══════════════════════════════════════════════════════════════
#  KONFIGURASI
# ══════════════════════════════════════════════════════════════

load_dotenv()

API_KEY     = os.getenv("BINANCE_API_KEY")
API_SECRET  = os.getenv("BINANCE_API_SECRET")
USE_TESTNET = os.getenv("TESTNET", "true").lower() == "true"
TG_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")

BASE_URL_LIVE    = "https://fapi.binance.com"
BASE_URL_TESTNET = "https://demo-fapi.binance.com"

# Trading params (bisa di-evolve otomatis)
DEFAULT_PAIRS    = ["BTCUSDT"]
INTERVAL         = "15m"
LEVERAGE         = 5
RISK_PERCENT     = 1.0
RR_RATIO         = 2.0
ATR_SL_MULT      = 1.5
MAX_OPEN_TRADES  = 3

# Dual agent intervals
SCANNER_INTERVAL_SEC = 60
MANAGER_INTERVAL_SEC = 15

# Indikator (bisa di-evolve)
EMA_FAST       = 20
EMA_SLOW       = 50
RSI_PERIOD     = 14
RSI_OVERSOLD   = 35
RSI_OVERBOUGHT = 65

# Lessons & Evolution
LESSONS_FILE         = "lessons.json"
THRESHOLDS_FILE      = "thresholds.json"
TRADE_HISTORY_FILE   = "trade_history.json"
MIN_TRADES_TO_EVOLVE = 10
LESSON_MIN_SAMPLE    = 3

# Trailing SL (Manager agent)
TRAILING_SL_ACTIVATE = 0.8   # aktif setelah profit 80% menuju TP
TRAILING_SL_DISTANCE = 0.4   # trail SL di 40% dari jarak entry-TP

# ══════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════

def tg_send(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=8
        )
    except Exception as e:
        log.warning(f"Telegram gagal: {e}")

def tg_trade(action, symbol, side, entry, sl, tp, qty, pnl=None, note=""):
    emoji = "🟢" if side == "LONG" else "🔴"
    body  = (
        f"{emoji} <b>{action} - {symbol}</b>\n\n"
        f"Side  : <b>{side}</b>\n"
        f"Entry : <code>{entry:.2f}</code>\n"
        f"SL    : <code>{sl:.2f}</code>\n"
        f"TP    : <code>{tp:.2f}</code>\n"
        f"Qty   : <code>{qty}</code>"
    )
    if pnl is not None:
        body += f"\nPnL   : <b>{'+' if pnl>=0 else ''}{pnl:.2f} USDT</b>"
    if note:
        body += f"\n<i>{note}</i>"
    tg_send(body)

# ══════════════════════════════════════════════════════════════
#  BINANCE CLIENT
# ══════════════════════════════════════════════════════════════

class BinanceClient:
    def __init__(self, api_key, api_secret, testnet=True):
        self.api_key    = api_key
        self.api_secret = api_secret
        self.base_url   = BASE_URL_TESTNET if testnet else BASE_URL_LIVE
        self.session    = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": self.api_key})
        log.info(f"BinanceClient - {'TESTNET' if testnet else 'LIVE'} | {self.base_url}")

    def _sign(self, params):
        params["timestamp"]  = int(time.time() * 1000)
        params["recvWindow"] = 5000
        query = urlencode(params)
        sig   = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        return params

    def _get(self, path, params=None, signed=False):
        if params is None: params = {}
        if signed: params = self._sign(params)
        r = self.session.get(f"{self.base_url}{path}", params=params, timeout=10)
        if not r.ok: raise Exception(f"GET {path} -> {r.status_code}: {r.text}")
        return r.json()

    def _post(self, path, params=None):
        if params is None: params = {}
        params = self._sign(params)
        r = self.session.post(
            f"{self.base_url}{path}",
            data=urlencode(params),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10
        )
        if not r.ok: raise Exception(f"POST {path} -> {r.status_code}: {r.text}")
        return r.json()

    def get_klines(self, symbol, interval, limit=300):
        return self._get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})

    def get_exchange_info(self):
        return self._get("/fapi/v1/exchangeInfo")

    def get_balance(self):
        return self._get("/fapi/v3/balance", signed=True)

    def get_positions(self):
        return self._get("/fapi/v3/positionRisk", signed=True)

    def set_leverage(self, symbol, leverage):
        return self._post("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

    def new_order(self, **kwargs):
        return self._post("/fapi/v1/order", kwargs)


def create_client():
    if not API_KEY or not API_SECRET:
        raise ValueError("BINANCE_API_KEY / BINANCE_API_SECRET kosong di .env!")
    client = BinanceClient(API_KEY, API_SECRET, testnet=USE_TESTNET)
    if not USE_TESTNET:
        log.warning("LIVE TRADING - uang sungguhan!")
        tg_send("LIVE Bot dimulai!")
    return client

# ══════════════════════════════════════════════════════════════
#  LESSONS SYSTEM
# ══════════════════════════════════════════════════════════════

def load_json(path, default):
    try:
        with open(path) as f: return json.load(f)
    except: return default

def save_json(path, data):
    with open(path, "w") as f: json.dump(data, f, indent=2, default=str)

def record_trade(symbol, side, entry, exit_price, result, pnl, conditions):
    history = load_json(TRADE_HISTORY_FILE, [])
    history.append({
        "ts": datetime.utcnow().isoformat(), "symbol": symbol,
        "side": side, "entry": entry, "exit": exit_price,
        "result": result, "pnl": pnl, "conditions": conditions
    })
    save_json(TRADE_HISTORY_FILE, history)
    log.info(f"Trade dicatat ke history ({result})")

def generate_lessons():
    history = load_json(TRADE_HISTORY_FILE, [])
    if len(history) < MIN_TRADES_TO_EVOLVE:
        log.info(f"Lessons: belum cukup data ({len(history)}/{MIN_TRADES_TO_EVOLVE})")
        return
    wins   = [t for t in history if t["result"] == "WIN"]
    losses = [t for t in history if t["result"] == "LOSS"]
    lessons = []
    now = datetime.utcnow().isoformat()

    # Pelajaran 1: Jam entry terbaik / terburuk
    from collections import Counter, defaultdict
    win_hours  = Counter([datetime.fromisoformat(t["ts"]).hour for t in wins])
    loss_hours = Counter([datetime.fromisoformat(t["ts"]).hour for t in losses])
    bad_hours  = [h for h, c in loss_hours.items()
                  if c >= LESSON_MIN_SAMPLE and c > win_hours.get(h, 0) * 1.5]
    good_hours = [h for h, c in win_hours.items()
                  if c >= LESSON_MIN_SAMPLE and c > loss_hours.get(h, 0) * 1.5]
    if bad_hours:
        lessons.append({"id": "hour_avoid", "title": "Hindari jam entry berisiko",
            "detail": f"Jam UTC loss rate tinggi: {sorted(bad_hours)}",
            "action": "skip_entry", "value": bad_hours, "updated": now})
    if good_hours:
        lessons.append({"id": "hour_prefer", "title": "Jam entry win rate tinggi",
            "detail": f"Jam UTC win rate tinggi: {sorted(good_hours)}",
            "action": "prefer_entry", "value": good_hours, "updated": now})

    # Pelajaran 2: RSI zone
    def avg_cond(trades, key):
        vals = [t["conditions"].get(key) for t in trades if t["conditions"].get(key) is not None]
        return sum(vals)/len(vals) if vals else None
    avg_rsi_win  = avg_cond(wins, "rsi")
    avg_rsi_loss = avg_cond(losses, "rsi")
    if avg_rsi_win and avg_rsi_loss and abs(avg_rsi_win - avg_rsi_loss) > 3:
        lessons.append({"id": "rsi_zone", "title": "RSI optimal saat entry",
            "detail": f"Avg RSI WIN={avg_rsi_win:.1f} vs LOSS={avg_rsi_loss:.1f}",
            "action": "info", "value": {"win": round(avg_rsi_win,1), "loss": round(avg_rsi_loss,1)},
            "updated": now})

    # Pelajaran 3: ATR filter
    avg_atr_win  = avg_cond(wins, "atr")
    avg_atr_loss = avg_cond(losses, "atr")
    if avg_atr_win and avg_atr_loss and avg_atr_loss > 0:
        if avg_atr_win / avg_atr_loss < 0.8:
            lessons.append({"id": "atr_high_risk", "title": "ATR tinggi meningkatkan risiko",
                "detail": f"ATR WIN={avg_atr_win:.1f} vs LOSS={avg_atr_loss:.1f}",
                "action": "atr_filter", "value": round(avg_atr_loss * 1.2, 1), "updated": now})

    # Pelajaran 4: Side bias per pair
    pair_wins = defaultdict(lambda: {"long": 0, "short": 0})
    for t in history:
        if t["result"] == "WIN":
            pair_wins[t["symbol"]][t["side"]] += 1
    for sym, sides in pair_wins.items():
        tl = sum(1 for t in history if t["symbol"]==sym and t["side"]=="long")
        ts = sum(1 for t in history if t["symbol"]==sym and t["side"]=="short")
        wrl = sides["long"]/tl*100  if tl >= LESSON_MIN_SAMPLE else None
        wrs = sides["short"]/ts*100 if ts >= LESSON_MIN_SAMPLE else None
        if wrl and wrs and abs(wrl-wrs) > 20:
            better = "long" if wrl > wrs else "short"
            lessons.append({"id": f"side_bias_{sym}",
                "title": f"Side bias {sym}",
                "detail": f"{sym} WR Long={wrl:.0f}% Short={wrs:.0f}% -> prefer {better.upper()}",
                "action": "side_bias", "value": {"symbol": sym, "prefer": better}, "updated": now})

    # Pelajaran 5: EMA spread filter
    avg_spread_win  = avg_cond(wins, "ema_spread_pct")
    avg_spread_loss = avg_cond(losses, "ema_spread_pct")
    if avg_spread_win and avg_spread_loss and avg_spread_win > avg_spread_loss * 1.3:
        lessons.append({"id": "ema_spread", "title": "EMA spread lebar = win rate lebih tinggi",
            "detail": f"Spread WIN={avg_spread_win:.3f}% vs LOSS={avg_spread_loss:.3f}%",
            "action": "ema_spread_filter", "value": round(avg_spread_loss, 4), "updated": now})

    save_json(LESSONS_FILE, lessons)
    log.info(f"{len(lessons)} lessons di-generate dari {len(history)} trade")
    tg_send(f"Lessons diperbarui\nDari {len(history)} trade -> {len(lessons)} pelajaran aktif")
    return lessons

def load_lessons():
    return load_json(LESSONS_FILE, [])

def apply_lessons_filter(signal, symbol, conditions):
    lessons  = load_lessons()
    hour_now = datetime.utcnow().hour
    for lesson in lessons:
        action = lesson.get("action")
        value  = lesson.get("value")
        if action == "skip_entry" and lesson["id"] == "hour_avoid":
            if hour_now in value:
                return "hold", f"Lesson: jam UTC {hour_now} loss rate tinggi"
        if action == "atr_filter":
            if conditions.get("atr", 0) > value:
                return "hold", f"Lesson: ATR={conditions.get('atr'):.1f} > threshold={value:.1f}"
        if action == "ema_spread_filter":
            if conditions.get("ema_spread_pct", 999) < value:
                return "hold", f"Lesson: EMA spread terlalu kecil"
        if action == "side_bias" and value.get("symbol") == symbol:
            if signal != value.get("prefer") and signal in ("long","short"):
                return "hold", f"Lesson: {symbol} prefer {value.get('prefer').upper()}"
    return signal, ""

def print_lessons():
    lessons = load_lessons()
    history = load_json(TRADE_HISTORY_FILE, [])
    print("\n" + "="*60)
    print(f"  LESSONS SYSTEM - {len(lessons)} pelajaran aktif")
    print(f"  Trade history  : {len(history)} trade tersimpan")
    print("="*60)
    if not lessons:
        print(f"  Belum ada lessons. Bot butuh minimal {MIN_TRADES_TO_EVOLVE} trade.\n")
        return
    for i, l in enumerate(lessons, 1):
        print(f"\n  [{i}] {l['title']}")
        print(f"       {l['detail']}")
        print(f"       Action: {l['action']} | Updated: {l.get('updated','?')[:10]}")
    print()

# ══════════════════════════════════════════════════════════════
#  THRESHOLD EVOLUTION
# ══════════════════════════════════════════════════════════════

def evolve_thresholds():
    global RSI_OVERSOLD, RSI_OVERBOUGHT, ATR_SL_MULT, RR_RATIO
    history = load_json(TRADE_HISTORY_FILE, [])
    if len(history) < MIN_TRADES_TO_EVOLVE:
        print(f"Butuh minimal {MIN_TRADES_TO_EVOLVE} trade (sekarang: {len(history)})")
        return
    wins   = [t for t in history if t["result"] == "WIN"]
    losses = [t for t in history if t["result"] == "LOSS"]
    wr     = len(wins) / len(history) * 100
    t      = load_json(THRESHOLDS_FILE, {
        "RSI_OVERSOLD": RSI_OVERSOLD, "RSI_OVERBOUGHT": RSI_OVERBOUGHT,
        "ATR_SL_MULT": ATR_SL_MULT, "RR_RATIO": RR_RATIO, "history": []
    })
    changes = []

    # Adjust RSI
    if wr < 45 and t["RSI_OVERSOLD"] > 28:
        new_os = max(28, t["RSI_OVERSOLD"] - 2)
        new_ob = min(72, t["RSI_OVERBOUGHT"] + 2)
        changes.append(f"RSI: {t['RSI_OVERSOLD']}/{t['RSI_OVERBOUGHT']} -> {new_os}/{new_ob} (WR rendah)")
        t["RSI_OVERSOLD"] = new_os; t["RSI_OVERBOUGHT"] = new_ob
    elif wr > 60 and len(history) >= 20 and t["RSI_OVERSOLD"] < 40:
        new_os = min(40, t["RSI_OVERSOLD"] + 1)
        new_ob = max(60, t["RSI_OVERBOUGHT"] - 1)
        changes.append(f"RSI: {t['RSI_OVERSOLD']}/{t['RSI_OVERBOUGHT']} -> {new_os}/{new_ob} (WR bagus)")
        t["RSI_OVERSOLD"] = new_os; t["RSI_OVERBOUGHT"] = new_ob

    # Adjust ATR
    sl_hits = sum(1 for x in losses if x["conditions"].get("exit_reason") == "SL")
    if losses and sl_hits > len(losses) * 0.7 and t["ATR_SL_MULT"] < 2.5:
        new_atr = round(min(2.5, t["ATR_SL_MULT"] + 0.1), 1)
        changes.append(f"ATR_SL_MULT: {t['ATR_SL_MULT']} -> {new_atr} (terlalu banyak SL hit)")
        t["ATR_SL_MULT"] = new_atr

    # Adjust RR
    avg_win  = sum(x["pnl"] for x in wins)  / len(wins)  if wins  else 0
    avg_loss = sum(x["pnl"] for x in losses) / len(losses) if losses else -1
    actual_rr = abs(avg_win / avg_loss) if avg_loss != 0 else t["RR_RATIO"]
    if actual_rr > t["RR_RATIO"] * 1.2 and wr > 50:
        new_rr = round(min(3.0, t["RR_RATIO"] + 0.1), 1)
        changes.append(f"RR_RATIO: {t['RR_RATIO']} -> {new_rr} (actual RR={actual_rr:.2f} lebih tinggi)")
        t["RR_RATIO"] = new_rr
    elif actual_rr < t["RR_RATIO"] * 0.7 and wr < 50:
        new_rr = round(max(1.5, t["RR_RATIO"] - 0.1), 1)
        changes.append(f"RR_RATIO: {t['RR_RATIO']} -> {new_rr} (actual RR={actual_rr:.2f} rendah)")
        t["RR_RATIO"] = new_rr

    t["history"].append({"ts": datetime.utcnow().isoformat(),
        "trades": len(history), "wr": round(wr,1), "changes": changes})
    save_json(THRESHOLDS_FILE, t)

    RSI_OVERSOLD = t["RSI_OVERSOLD"]; RSI_OVERBOUGHT = t["RSI_OVERBOUGHT"]
    ATR_SL_MULT  = t["ATR_SL_MULT"];  RR_RATIO       = t["RR_RATIO"]

    print("\n" + "="*60)
    print(f"  THRESHOLD EVOLUTION - {len(history)} trade dianalisis")
    print(f"  Win Rate: {wr:.1f}%")
    print("-"*60)
    if changes:
        for c in changes: print(f"  -> {c}")
    else:
        print("  Tidak ada perubahan. Parameter sudah optimal.")
    print(f"\n  RSI    : {RSI_OVERSOLD}/{RSI_OVERBOUGHT}")
    print(f"  ATR    : {ATR_SL_MULT}")
    print(f"  RR     : {RR_RATIO}")
    print("="*60 + "\n")
    if changes:
        tg_send("Threshold Evolution\n" + "\n".join(f"- {c}" for c in changes))
    generate_lessons()

def load_thresholds():
    global RSI_OVERSOLD, RSI_OVERBOUGHT, ATR_SL_MULT, RR_RATIO
    t = load_json(THRESHOLDS_FILE, {})
    if t:
        RSI_OVERSOLD   = t.get("RSI_OVERSOLD",   RSI_OVERSOLD)
        RSI_OVERBOUGHT = t.get("RSI_OVERBOUGHT",  RSI_OVERBOUGHT)
        ATR_SL_MULT    = t.get("ATR_SL_MULT",     ATR_SL_MULT)
        RR_RATIO       = t.get("RR_RATIO",        RR_RATIO)
        log.info(f"Thresholds loaded: RSI={RSI_OVERSOLD}/{RSI_OVERBOUGHT} ATR={ATR_SL_MULT} RR={RR_RATIO}")

# ══════════════════════════════════════════════════════════════
#  DATA & INDIKATOR
# ══════════════════════════════════════════════════════════════

def get_klines(client, symbol, interval=INTERVAL, limit=300):
    raw = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df  = pd.DataFrame(raw, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","quote_volume","trades","taker_buy_base","taker_buy_quote","ignore"
    ])
    for c in ["open","high","low","close","volume"]: df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df["ema_fast"]  = ta.trend.ema_indicator(df["close"], window=EMA_FAST)
    df["ema_slow"]  = ta.trend.ema_indicator(df["close"], window=EMA_SLOW)
    df["rsi"]       = ta.momentum.rsi(df["close"], window=RSI_PERIOD)
    df["atr"]       = ta.volatility.average_true_range(df["high"], df["low"], df["close"])
    return df.dropna().reset_index(drop=True)

def build_conditions(row):
    spread = abs(row.ema_fast - row.ema_slow) / row.ema_slow * 100
    return {
        "rsi": round(float(row.rsi), 2), "atr": round(float(row.atr), 2),
        "ema_fast": round(float(row.ema_fast), 2), "ema_slow": round(float(row.ema_slow), 2),
        "ema_spread_pct": round(spread, 4),
        "hour_utc": datetime.utcnow().hour, "weekday": datetime.utcnow().weekday()
    }

def get_signal(df):
    c = df.iloc[-1]; p = df.iloc[-2]
    long_ok  = (c.close > c.ema_slow and c.ema_fast > c.ema_slow and c.rsi < RSI_OVERSOLD  and p.rsi < c.rsi)
    short_ok = (c.close < c.ema_slow and c.ema_fast < c.ema_slow and c.rsi > RSI_OVERBOUGHT and p.rsi > c.rsi)
    cond = build_conditions(c)
    if long_ok:  return "long",  cond
    if short_ok: return "short", cond
    return "hold", cond

# ══════════════════════════════════════════════════════════════
#  ORDER & POSISI
# ══════════════════════════════════════════════════════════════

def get_balance(client):
    for b in client.get_balance():
        if b["asset"] == "USDT": return float(b["availableBalance"])
    return 0.0

def get_open_positions(client):
    result = {}
    for pos in client.get_positions():
        if float(pos["positionAmt"]) != 0: result[pos["symbol"]] = pos
    return result

def set_leverage(client, symbol):
    try:
        client.set_leverage(symbol=symbol, leverage=LEVERAGE)
        log.info(f"Leverage {symbol} -> {LEVERAGE}x")
    except Exception as e:
        log.warning(f"Set leverage {symbol}: {e}")

def get_lot_step(client, symbol):
    for s in client.get_exchange_info()["symbols"]:
        if s["symbol"] == symbol:
            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE": return float(f["stepSize"])
    return 0.001

def calculate_qty(client, symbol, price, sl_price):
    balance   = get_balance(client)
    risk_usdt = balance * (RISK_PERCENT / 100)
    sl_dist   = abs(price - sl_price)
    if sl_dist == 0: return 0.0
    qty  = (risk_usdt * LEVERAGE) / sl_dist
    step = get_lot_step(client, symbol)
    return max(round(qty - (qty % step), 8), step)

# Shared state antar thread
_open_trade_meta = {}
_state_lock = threading.Lock()

def open_trade(client, symbol, signal, price, atr, conditions):
    sl_price = (price - atr * ATR_SL_MULT) if signal == "long" else (price + atr * ATR_SL_MULT)
    sl_dist  = abs(price - sl_price)
    tp_price = (price + sl_dist * RR_RATIO) if signal == "long" else (price - sl_dist * RR_RATIO)
    side     = "BUY"  if signal == "long" else "SELL"
    sl_side  = "SELL" if signal == "long" else "BUY"
    qty      = calculate_qty(client, symbol, price, sl_price)
    if qty <= 0: log.warning(f"[{symbol}] Qty=0, skip"); return
    try:
        client.new_order(symbol=symbol, side=side, type="MARKET", quantity=qty)
        log.info(f"OPEN {signal.upper()} {symbol} qty={qty} @ ~{price:.2f}")
        client.new_order(symbol=symbol, side=sl_side, type="STOP_MARKET",
                         stopPrice=round(sl_price,2), closePosition="true")
        client.new_order(symbol=symbol, side=sl_side, type="TAKE_PROFIT_MARKET",
                         stopPrice=round(tp_price,2), closePosition="true")
        with _state_lock:
            _open_trade_meta[symbol] = {
                "entry": price, "sl": sl_price, "tp": tp_price,
                "side": signal, "qty": qty, "conditions": conditions, "trailing_sl": None
            }
        tg_trade("OPEN TRADE", symbol, signal.upper(), price, sl_price, tp_price, qty)
    except Exception as e:
        log.error(f"Order {symbol}: {e}"); tg_send(f"Order gagal {symbol}: {e}")

def close_trade(client, symbol, pos, reason="", exit_price=None):
    amt  = float(pos["positionAmt"])
    side = "SELL" if amt > 0 else "BUY"
    pnl  = float(pos.get("unrealizedProfit", 0))
    try:
        client.new_order(symbol=symbol, side=side, type="MARKET",
                         quantity=abs(amt), reduceOnly="true")
        log.info(f"CLOSE {symbol} PnL={pnl:+.2f} [{reason}]")
        tg_send(f"CLOSE {symbol}\nReason: {reason}\nPnL: {pnl:+.2f} USDT")
        with _state_lock: meta = _open_trade_meta.pop(symbol, {})
        if meta:
            cond = meta.get("conditions", {}); cond["exit_reason"] = reason
            record_trade(symbol, meta.get("side","?"), meta.get("entry",0),
                         exit_price or meta.get("tp",0),
                         "WIN" if pnl > 0 else "LOSS", pnl, cond)
            history = load_json(TRADE_HISTORY_FILE, [])
            if len(history) % MIN_TRADES_TO_EVOLVE == 0:
                log.info("Auto-evolving thresholds..."); evolve_thresholds()
    except Exception as e:
        log.error(f"Close {symbol}: {e}")

# ══════════════════════════════════════════════════════════════
#  DUAL AGENT
# ══════════════════════════════════════════════════════════════

def agent_scanner(client, pairs, stop_event):
    log.info("Scanner Agent dimulai")
    while not stop_event.is_set():
        try:
            open_pos = get_open_positions(client)
            balance  = get_balance(client)
            log.info(f"[SCANNER] Balance={balance:.2f} USDT | Open={len(open_pos)}/{MAX_OPEN_TRADES}")
            for symbol in pairs:
                try:
                    df                 = get_klines(client, symbol)
                    signal, conditions = get_signal(df)
                    last               = df.iloc[-1]
                    price              = float(last["close"])
                    atr                = float(last["atr"])
                    signal_final, skip = apply_lessons_filter(signal, symbol, conditions)
                    log.info(
                        f"[SCANNER][{symbol}] {price:.2f} | RSI={last.rsi:.1f} | "
                        f"Raw={signal.upper()} -> Final={signal_final.upper()}"
                        + (f" | SKIP: {skip}" if skip else "")
                    )
                    if symbol in open_pos:
                        pos_side = "long" if float(open_pos[symbol]["positionAmt"]) > 0 else "short"
                        if (pos_side=="long" and signal_final=="short") or \
                           (pos_side=="short" and signal_final=="long"):
                            close_trade(client, symbol, open_pos[symbol], "sinyal berlawanan", price)
                            del open_pos[symbol]
                            if len(open_pos) < MAX_OPEN_TRADES:
                                open_trade(client, symbol, signal_final, price, atr, conditions)
                    elif signal_final in ("long","short"):
                        if len(open_pos) >= MAX_OPEN_TRADES:
                            log.info(f"[SCANNER] Max posisi, skip {symbol}")
                        else:
                            open_trade(client, symbol, signal_final, price, atr, conditions)
                            open_pos[symbol] = True
                except Exception as e:
                    log.error(f"[SCANNER][{symbol}] {e}")
        except Exception as e:
            log.error(f"[SCANNER] {e}", exc_info=True)
        stop_event.wait(SCANNER_INTERVAL_SEC)
    log.info("Scanner Agent berhenti")

def agent_manager(client, pairs, stop_event):
    log.info("Manager Agent dimulai")
    while not stop_event.is_set():
        try:
            open_pos = get_open_positions(client)
            if not open_pos: stop_event.wait(MANAGER_INTERVAL_SEC); continue
            for symbol, pos in open_pos.items():
                try:
                    upnl  = float(pos.get("unrealizedProfit", 0))
                    entry = float(pos.get("entryPrice", 0))
                    with _state_lock: meta = _open_trade_meta.get(symbol)
                    if not meta: continue
                    tp_dist     = abs(meta["tp"] - entry)
                    cur_df      = get_klines(client, symbol, limit=5)
                    cur_price   = float(cur_df.iloc[-1]["close"])
                    profit_dist = abs(cur_price - entry)
                    log.info(
                        f"[MANAGER][{symbol}] {meta['side'].upper()} | "
                        f"uPnL={upnl:+.2f} | "
                        f"Progress={profit_dist/tp_dist*100:.0f}% ke TP" if tp_dist > 0 else
                        f"[MANAGER][{symbol}] uPnL={upnl:+.2f}"
                    )
                    # Trailing SL
                    if tp_dist > 0 and profit_dist / tp_dist >= TRAILING_SL_ACTIVATE:
                        trail_dist  = tp_dist * TRAILING_SL_DISTANCE
                        new_sl      = round((cur_price - trail_dist) if meta["side"]=="long"
                                            else (cur_price + trail_dist), 2)
                        current_tsl = meta.get("trailing_sl")
                        improved    = (meta["side"]=="long"  and (current_tsl is None or new_sl > current_tsl)) or \
                                      (meta["side"]=="short" and (current_tsl is None or new_sl < current_tsl))
                        if improved:
                            log.info(f"[MANAGER][{symbol}] Trailing SL: {current_tsl} -> {new_sl}")
                            with _state_lock: _open_trade_meta[symbol]["trailing_sl"] = new_sl
                            tg_send(f"Trailing SL {symbol}\nSL baru: {new_sl}\nuPnL: {upnl:+.2f} USDT")
                except Exception as e:
                    log.error(f"[MANAGER][{symbol}] {e}")
        except Exception as e:
            log.error(f"[MANAGER] {e}")
        stop_event.wait(MANAGER_INTERVAL_SEC)
    log.info("Manager Agent berhenti")

# ══════════════════════════════════════════════════════════════
#  BACKTEST
# ══════════════════════════════════════════════════════════════

def run_backtest(pairs, days=90):
    print("\n" + "="*60)
    print("  RRL-WEALTH BACKTEST ENGINE v2.0")
    print(f"  Pairs: {', '.join(pairs)} | Period: {days} hari")
    print("="*60 + "\n")
    client = BinanceClient("", "", testnet=False)
    total  = {"trades":0,"wins":0,"losses":0,"total_pnl":0.0,"max_dd":0.0}
    for symbol in pairs:
        print(f"Backtesting {symbol}...")
        try:
            df = get_klines(client, symbol, INTERVAL, limit=min(days*96, 1500))
        except Exception as e:
            print(f"  Gagal: {e}\n"); continue
        balance = peak = 1000.0; max_dd = 0.0; trade_log = []
        in_trade = False; entry_price = sl_price = tp_price = 0.0; trade_side = ""
        for i in range(50, len(df)):
            c = df.iloc[i]; p = df.iloc[i-1]
            if not in_trade:
                lo = (c.close > c.ema_slow and c.ema_fast > c.ema_slow and c.rsi < RSI_OVERSOLD  and p.rsi < c.rsi)
                so = (c.close < c.ema_slow and c.ema_fast < c.ema_slow and c.rsi > RSI_OVERBOUGHT and p.rsi > c.rsi)
                if lo or so:
                    trade_side = "long" if lo else "short"; entry_price = c.close
                    sl_dist    = c.atr * ATR_SL_MULT
                    sl_price   = (entry_price-sl_dist) if trade_side=="long" else (entry_price+sl_dist)
                    tp_price   = (entry_price+sl_dist*RR_RATIO) if trade_side=="long" else (entry_price-sl_dist*RR_RATIO)
                    in_trade   = True
            else:
                hit_tp = (trade_side=="long" and c.high>=tp_price) or (trade_side=="short" and c.low<=tp_price)
                hit_sl = (trade_side=="long" and c.low<=sl_price)  or (trade_side=="short" and c.high>=sl_price)
                if hit_tp or hit_sl:
                    risk = balance*(RISK_PERCENT/100); pnl = risk*RR_RATIO if hit_tp else -risk
                    balance += pnl; peak = max(peak, balance)
                    dd = (peak-balance)/peak*100; max_dd = max(max_dd, dd)
                    trade_log.append({"result":"WIN" if hit_tp else "LOSS","pnl":pnl}); in_trade=False
        if not trade_log: print("  Tidak ada trade\n"); continue
        tdf  = pd.DataFrame(trade_log)
        wins = (tdf["result"]=="WIN").sum(); ttl = len(tdf)
        wr   = wins/ttl*100; net = tdf["pnl"].sum(); ret = (balance-1000)/1000*100
        pf   = tdf[tdf.pnl>0]["pnl"].sum() / abs(tdf[tdf.pnl<0]["pnl"].sum()+1e-9)
        print(f"  Trades   : {ttl}")
        print(f"  Win Rate : {wr:.1f}%  ({wins}W / {ttl-wins}L)")
        print(f"  Profit F : {pf:.2f}x")
        print(f"  Net PnL  : {net:+.2f} USDT  ({ret:+.1f}%)")
        print(f"  Max DD   : {max_dd:.1f}%")
        print(f"  Balance  : $1000 -> ${balance:.2f}\n")
        total["trades"] += ttl; total["wins"] += wins; total["losses"] += ttl-wins
        total["total_pnl"] += net; total["max_dd"] = max(total["max_dd"], max_dd)
    if len(pairs) > 1:
        wr = total["wins"]/total["trades"]*100 if total["trades"] > 0 else 0
        print(f"  RINGKASAN: {total['trades']} trades | WR={wr:.1f}% | PnL={total['total_pnl']:+.2f} | DD={total['max_dd']:.1f}%")
    print("="*60 + "\n")

# ══════════════════════════════════════════════════════════════
#  LIVE BOT
# ══════════════════════════════════════════════════════════════

def run_live(pairs):
    load_thresholds()
    history = load_json(TRADE_HISTORY_FILE, [])
    log.info("="*60)
    log.info("RRL-WEALTH TRADING BOT v2.0")
    log.info(f"Scanner: {SCANNER_INTERVAL_SEC}s | Manager: {MANAGER_INTERVAL_SEC}s")
    log.info(f"Pairs: {', '.join(pairs)} | Leverage: {LEVERAGE}x | Risk: {RISK_PERCENT}%")
    log.info(f"RSI: {RSI_OVERSOLD}/{RSI_OVERBOUGHT} | ATR: {ATR_SL_MULT} | RR: {RR_RATIO}")
    log.info(f"Trade history: {len(history)} trade tersimpan")
    log.info("="*60)
    tg_send(
        f"RRL-Wealth v2.0 dimulai\n"
        f"Pairs: {', '.join(pairs)}\n"
        f"Mode: {'TESTNET' if USE_TESTNET else 'LIVE'}\n"
        f"RSI: {RSI_OVERSOLD}/{RSI_OVERBOUGHT} | ATR: {ATR_SL_MULT} | RR: {RR_RATIO}\n"
        f"History: {len(history)} trade"
    )
    client = create_client()
    for p in pairs: set_leverage(client, p)
    stop_event = threading.Event()
    scanner = threading.Thread(target=agent_scanner, args=(client, pairs, stop_event), daemon=True)
    manager = threading.Thread(target=agent_manager, args=(client, pairs, stop_event), daemon=True)
    scanner.start(); manager.start()
    log.info("Dual Agent aktif - tekan Ctrl+C untuk berhenti\n")
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        log.info("Menghentikan bot...")
        stop_event.set(); scanner.join(timeout=10); manager.join(timeout=10)
        log.info("Bot dihentikan.")
        tg_send("RRL-Wealth dihentikan manual")

# ══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RRL-Wealth Trading Bot v2.0")
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--evolve",   action="store_true")
    parser.add_argument("--lessons",  action="store_true")
    parser.add_argument("--days",     type=int, default=90)
    parser.add_argument("--pairs",    nargs="+", default=DEFAULT_PAIRS)
    args = parser.parse_args()
    if args.backtest:  run_backtest(args.pairs, args.days)
    elif args.evolve:  evolve_thresholds()
    elif args.lessons: print_lessons()
    else:              run_live(args.pairs)
