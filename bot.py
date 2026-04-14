"""
╔══════════════════════════════════════════════════════════════╗
║              RRL-WEALTH TRADING BOT v1.0                     ║
║       Strategi : RSI + EMA Confluence                        ║
║       Fitur    : Live Trading, Backtest, Telegram Notif      ║
║       API      : Binance USD-M Futures (Official SDK)        ║
╚══════════════════════════════════════════════════════════════╝

BASE URL:
    Live    : https://fapi.binance.com
    Testnet : https://demo-fapi.binance.com

DEPENDENCIES:
    pip install binance-futures-connector pandas ta python-dotenv requests

FILE .env (wajib ada di folder yang sama):
    BINANCE_API_KEY=xxx
    BINANCE_API_SECRET=xxx
    TESTNET=true
    TELEGRAM_TOKEN=xxx        # dari @BotFather
    TELEGRAM_CHAT_ID=xxx      # dari @userinfobot

CARA PAKAI:
    python bot.py             → jalankan live bot
    python bot.py --backtest  → jalankan backtest dulu
    python bot.py --pairs BTCUSDT ETHUSDT SOLUSDT  → multi-pair
"""

import os
import sys
import time
import hmac
import hashlib
import logging
import argparse
import requests
from urllib.parse import urlencode
from datetime import datetime, timedelta
from dotenv import load_dotenv
import pandas as pd
import ta

# ══════════════════════════════════════════════════════════════
#  KONFIGURASI — edit sesuai kebutuhan
# ══════════════════════════════════════════════════════════════

load_dotenv()

API_KEY        = os.getenv("BINANCE_API_KEY")
API_SECRET     = os.getenv("BINANCE_API_SECRET")
USE_TESTNET    = os.getenv("TESTNET", "true").lower() == "true"
TG_TOKEN       = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Base URL sesuai docs resmi Binance USD-M Futures ──────────
BASE_URL_LIVE    = "https://fapi.binance.com"
BASE_URL_TESTNET = "https://demo-fapi.binance.com"

# ── Trading params ────────────────────────────────────────────
DEFAULT_PAIRS   = ["BTCUSDT"]          # bisa dioverride via --pairs
INTERVAL        = "15m"                # timeframe candle
LEVERAGE        = 5
RISK_PERCENT    = 1.0                  # % balance per trade
RR_RATIO        = 2.0                  # risk:reward 1:2
ATR_SL_MULT     = 1.5                  # SL = entry ± ATR × mult
MAX_OPEN_TRADES = 3                    # maks posisi bersamaan
SCAN_EVERY_SEC  = 60                   # interval scan (detik)

# ── Indikator ─────────────────────────────────────────────────
EMA_FAST        = 20
EMA_SLOW        = 50
RSI_PERIOD      = 14
RSI_OVERSOLD    = 35
RSI_OVERBOUGHT  = 65

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

def tg_send(msg: str):
    """Kirim notifikasi ke Telegram. Diam-diam jika token kosong."""
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, data={
            "chat_id": TG_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=8)
    except Exception as e:
        log.warning(f"Telegram gagal: {e}")


def tg_trade(action: str, symbol: str, side: str,
             entry: float, sl: float, tp: float,
             qty: float, pnl: float = None):
    """Format pesan trade untuk Telegram."""
    emoji  = "🟢" if side == "LONG" else "🔴"
    header = f"{emoji} <b>{action} — {symbol}</b>"
    body   = (
        f"Side   : <b>{side}</b>\n"
        f"Entry  : <code>{entry:.2f}</code>\n"
        f"SL     : <code>{sl:.2f}</code>\n"
        f"TP     : <code>{tp:.2f}</code>\n"
        f"Qty    : <code>{qty}</code>"
    )
    if pnl is not None:
        sign = "+" if pnl >= 0 else ""
        body += f"\nPnL    : <b>{sign}{pnl:.2f} USDT</b>"
    tg_send(f"{header}\n\n{body}")

# ══════════════════════════════════════════════════════════════
#  BINANCE USD-M FUTURES CLIENT
#  Menggunakan REST API langsung sesuai dokumentasi resmi:
#  Base URL  live    : https://fapi.binance.com
#  Base URL  testnet : https://demo-fapi.binance.com
#  Auth      : HMAC SHA256 signature pada SIGNED endpoints
#  API Key   : dikirim via header X-MBX-APIKEY
# ══════════════════════════════════════════════════════════════

class BinanceClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.api_key    = api_key
        self.api_secret = api_secret
        self.base_url   = BASE_URL_TESTNET if testnet else BASE_URL_LIVE
        self.session    = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": self.api_key})
        mode = "TESTNET" if testnet else "LIVE"
        log.info(f"🔗 BinanceClient — {mode} | {self.base_url}")

    def _sign(self, params: dict) -> dict:
        """Tambahkan timestamp + HMAC SHA256 signature ke params."""
        params["timestamp"]  = int(time.time() * 1000)
        params["recvWindow"] = 5000
        query = urlencode(params)
        sig   = hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        params["signature"] = sig
        return params

    def _get(self, path: str, params: dict = None, signed: bool = False):
        if params is None:
            params = {}
        if signed:
            params = self._sign(params)
        r = self.session.get(f"{self.base_url}{path}", params=params, timeout=10)
        if not r.ok:
            raise Exception(f"GET {path} → {r.status_code}: {r.text}")
        return r.json()

    def _post(self, path: str, params: dict = None):
        if params is None:
            params = {}
        params = self._sign(params)
        r = self.session.post(
            f"{self.base_url}{path}",
            data=urlencode(params),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10
        )
        if not r.ok:
            raise Exception(f"POST {path} → {r.status_code}: {r.text}")
        return r.json()

    # ── Market Data (no auth) ─────────────────────────────────
    def get_klines(self, symbol: str, interval: str, limit: int = 300):
        return self._get("/fapi/v1/klines", {
            "symbol": symbol, "interval": interval, "limit": limit
        })

    def get_exchange_info(self):
        return self._get("/fapi/v1/exchangeInfo")

    # ── Account (SIGNED) ──────────────────────────────────────
    def get_balance(self):
        return self._get("/fapi/v3/balance", signed=True)

    def get_positions(self):
        return self._get("/fapi/v3/positionRisk", signed=True)

    # ── Trade (SIGNED) ────────────────────────────────────────
    def set_leverage(self, symbol: str, leverage: int):
        return self._post("/fapi/v1/leverage", {
            "symbol": symbol, "leverage": leverage
        })

    def new_order(self, **kwargs):
        return self._post("/fapi/v1/order", kwargs)


def create_client() -> BinanceClient:
    if not API_KEY or not API_SECRET:
        raise ValueError("BINANCE_API_KEY / BINANCE_API_SECRET kosong di .env!")
    client = BinanceClient(API_KEY, API_SECRET, testnet=USE_TESTNET)
    if not USE_TESTNET:
        log.warning("⚠️  LIVE TRADING — uang sungguhan!")
        tg_send("⚠️ <b>Bot LIVE dimulai!</b>")
    return client

# ══════════════════════════════════════════════════════════════
#  DATA & INDIKATOR
# ══════════════════════════════════════════════════════════════

def get_klines(client: BinanceClient, symbol: str,
               interval: str = INTERVAL, limit: int = 300) -> pd.DataFrame:
    raw = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df  = pd.DataFrame(raw, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","quote_volume","trades",
        "taker_buy_base","taker_buy_quote","ignore"
    ])
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")

    df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=EMA_FAST)
    df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=EMA_SLOW)
    df["rsi"]      = ta.momentum.rsi(df["close"], window=RSI_PERIOD)
    df["atr"]      = ta.volatility.average_true_range(
                         df["high"], df["low"], df["close"])
    return df.dropna().reset_index(drop=True)


def get_signal(df: pd.DataFrame) -> str:
    """
    LONG  : close > EMA50, EMA20 > EMA50, RSI naik dari < 35
    SHORT : close < EMA50, EMA20 < EMA50, RSI turun dari > 65
    """
    c = df.iloc[-1]
    p = df.iloc[-2]

    long_ok  = (c.close > c.ema_slow and c.ema_fast > c.ema_slow
                and c.rsi < RSI_OVERSOLD and p.rsi < c.rsi)
    short_ok = (c.close < c.ema_slow and c.ema_fast < c.ema_slow
                and c.rsi > RSI_OVERBOUGHT and p.rsi > c.rsi)

    if long_ok:   return "long"
    if short_ok:  return "short"
    return "hold"

# ══════════════════════════════════════════════════════════════
#  ORDER & POSISI
# ══════════════════════════════════════════════════════════════

def get_balance(client: BinanceClient) -> float:
    for b in client.get_balance():
        if b["asset"] == "USDT":
            return float(b["availableBalance"])
    return 0.0


def get_open_positions(client: BinanceClient) -> dict:
    """Kembalikan dict {symbol: position_info}."""
    result = {}
    for pos in client.get_positions():
        if float(pos["positionAmt"]) != 0:
            result[pos["symbol"]] = pos
    return result


def set_leverage(client: BinanceClient, symbol: str):
    try:
        client.set_leverage(symbol=symbol, leverage=LEVERAGE)
        log.info(f"Leverage {symbol} → {LEVERAGE}x")
    except Exception as e:
        log.warning(f"Set leverage {symbol}: {e}")


def get_lot_step(client: BinanceClient, symbol: str) -> float:
    info = client.get_exchange_info()
    for s in info["symbols"]:
        if s["symbol"] == symbol:
            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    return float(f["stepSize"])
    return 0.001


def calculate_qty(client: BinanceClient, symbol: str,
                  price: float, sl_price: float) -> float:
    balance    = get_balance(client)
    risk_usdt  = balance * (RISK_PERCENT / 100)
    sl_dist    = abs(price - sl_price)
    if sl_dist == 0:
        return 0.0
    qty  = (risk_usdt * LEVERAGE) / sl_dist
    step = get_lot_step(client, symbol)
    qty  = round(qty - (qty % step), 8)
    return max(qty, step)


def open_trade(client: BinanceClient, symbol: str, signal: str,
               price: float, atr: float):
    sl_price = (price - atr * ATR_SL_MULT) if signal == "long" \
               else (price + atr * ATR_SL_MULT)
    sl_dist  = abs(price - sl_price)
    tp_price = (price + sl_dist * RR_RATIO) if signal == "long" \
               else (price - sl_dist * RR_RATIO)
    side     = "BUY" if signal == "long" else "SELL"
    sl_side  = "SELL" if signal == "long" else "BUY"
    qty      = calculate_qty(client, symbol, price, sl_price)

    if qty <= 0:
        log.warning(f"[{symbol}] Qty=0, skip")
        return

    try:
        client.new_order(
            symbol=symbol, side=side, type="MARKET", quantity=qty)
        log.info(f"✅ OPEN {signal.upper()} {symbol} qty={qty} @ ~{price:.2f}")

        client.new_order(
            symbol=symbol, side=sl_side, type="STOP_MARKET",
            stopPrice=round(sl_price, 2), closePosition="true")

        client.new_order(
            symbol=symbol, side=sl_side, type="TAKE_PROFIT_MARKET",
            stopPrice=round(tp_price, 2), closePosition="true")

        tg_trade("OPEN TRADE", symbol, signal.upper(),
                 price, sl_price, tp_price, qty)

    except Exception as e:
        log.error(f"❌ Order {symbol}: {e}")
        tg_send(f"❌ <b>Order gagal</b> {symbol}: {e}")


def close_trade(client: BinanceClient, symbol: str, pos: dict, reason: str = ""):
    amt  = float(pos["positionAmt"])
    side = "SELL" if amt > 0 else "BUY"
    pnl  = float(pos.get("unrealizedProfit", 0))
    try:
        client.new_order(
            symbol=symbol, side=side, type="MARKET",
            quantity=abs(amt), reduceOnly="true")
        log.info(f"🔒 CLOSE {symbol} PnL={pnl:+.2f} USDT [{reason}]")
        tg_send(
            f"🔒 <b>CLOSE {symbol}</b>\n"
            f"Reason : {reason}\n"
            f"PnL    : <b>{pnl:+.2f} USDT</b>"
        )
    except Exception as e:
        log.error(f"❌ Close {symbol}: {e}")

# ══════════════════════════════════════════════════════════════
#  BACKTEST
# ══════════════════════════════════════════════════════════════

def run_backtest(pairs: list, days: int = 90):
    """
    Backtest: simulasi sinyal + SL/TP pada data historis.
    Pakai Binance public endpoint (tidak butuh auth).
    """
    print("\n" + "═"*60)
    print("  BACKTEST MODE")
    print(f"  Pairs : {', '.join(pairs)}")
    print(f"  Period: {days} hari terakhir")
    print("═"*60 + "\n")

    # Client tanpa auth — klines adalah public endpoint
    client = BinanceClient("", "", testnet=False)

    total_stats = {"trades": 0, "wins": 0, "losses": 0,
                   "total_pnl": 0.0, "max_dd": 0.0}

    for symbol in pairs:
        print(f"▸ Backtesting {symbol}...")
        try:
            limit = min(days * 96, 1500)
            df = get_klines(client, symbol, INTERVAL, limit=limit)
        except Exception as e:
            print(f"  ⚠ Gagal ambil data {symbol}: {e}")
            continue

        balance      = 1000.0
        peak_balance = balance
        max_dd       = 0.0
        trade_log    = []
        in_trade     = False
        entry_price  = 0.0
        sl_price     = 0.0
        tp_price     = 0.0
        trade_side   = ""

        for i in range(50, len(df)):
            row  = df.iloc[i]
            prev = df.iloc[i-1]
            c    = row
            p    = prev

            if not in_trade:
                long_ok  = (c.close > c.ema_slow and c.ema_fast > c.ema_slow
                            and c.rsi < RSI_OVERSOLD and p.rsi < c.rsi)
                short_ok = (c.close < c.ema_slow and c.ema_fast < c.ema_slow
                            and c.rsi > RSI_OVERBOUGHT and p.rsi > c.rsi)

                if long_ok or short_ok:
                    trade_side  = "long" if long_ok else "short"
                    entry_price = c.close
                    sl_dist     = c.atr * ATR_SL_MULT
                    sl_price    = (entry_price - sl_dist) if trade_side == "long" \
                                  else (entry_price + sl_dist)
                    tp_price    = (entry_price + sl_dist * RR_RATIO) if trade_side == "long" \
                                  else (entry_price - sl_dist * RR_RATIO)
                    in_trade    = True

            else:
                hit_tp = (trade_side == "long"  and c.high >= tp_price) or \
                         (trade_side == "short" and c.low  <= tp_price)
                hit_sl = (trade_side == "long"  and c.low  <= sl_price) or \
                         (trade_side == "short" and c.high >= sl_price)

                if hit_tp or hit_sl:
                    sl_dist    = abs(entry_price - sl_price)
                    risk_usdt  = balance * (RISK_PERCENT / 100)
                    pnl        = risk_usdt * RR_RATIO if hit_tp else -risk_usdt

                    balance   += pnl
                    peak_balance = max(peak_balance, balance)
                    dd = (peak_balance - balance) / peak_balance * 100
                    max_dd = max(max_dd, dd)

                    trade_log.append({
                        "time"  : c.open_time,
                        "side"  : trade_side,
                        "entry" : entry_price,
                        "exit"  : tp_price if hit_tp else sl_price,
                        "result": "WIN" if hit_tp else "LOSS",
                        "pnl"   : pnl,
                    })
                    in_trade = False

        # Statistik per pair
        if not trade_log:
            print(f"  → Tidak ada trade tergenerate di periode ini\n")
            continue

        tdf   = pd.DataFrame(trade_log)
        wins  = (tdf["result"] == "WIN").sum()
        total = len(tdf)
        wr    = wins / total * 100
        pf    = tdf[tdf.pnl > 0]["pnl"].sum() / abs(tdf[tdf.pnl < 0]["pnl"].sum() + 1e-9)
        net   = tdf["pnl"].sum()
        ret   = (balance - 1000) / 1000 * 100

        print(f"  Trades    : {total}")
        print(f"  Win Rate  : {wr:.1f}%  ({wins}W / {total-wins}L)")
        print(f"  Profit F  : {pf:.2f}x")
        print(f"  Net PnL   : {net:+.2f} USDT  ({ret:+.1f}%)")
        print(f"  Max DD    : {max_dd:.1f}%")
        print(f"  Balance   : $1000 → ${balance:.2f}")
        print()

        total_stats["trades"]    += total
        total_stats["wins"]      += wins
        total_stats["losses"]    += total - wins
        total_stats["total_pnl"] += net
        total_stats["max_dd"]     = max(total_stats["max_dd"], max_dd)

    if len(pairs) > 1:
        print("─"*60)
        print("  RINGKASAN SEMUA PAIR")
        t = total_stats
        wr = t["wins"] / t["trades"] * 100 if t["trades"] > 0 else 0
        print(f"  Total Trades : {t['trades']}")
        print(f"  Win Rate     : {wr:.1f}%")
        print(f"  Net PnL      : {t['total_pnl']:+.2f} USDT")
        print(f"  Max Drawdown : {t['max_dd']:.1f}%")
    print("═"*60 + "\n")

# ══════════════════════════════════════════════════════════════
#  LIVE BOT — MULTI-PAIR
# ══════════════════════════════════════════════════════════════

def run_live(pairs: list):
    log.info("═"*60)
    log.info("💰  RRL-WEALTH TRADING BOT v1.0")
    log.info("🤖  MULTI PAIR DIMULAI")
    log.info(f"    Pairs    : {', '.join(pairs)}")
    log.info(f"    Leverage : {LEVERAGE}x | Risk : {RISK_PERCENT}%/trade")
    log.info(f"    Max open : {MAX_OPEN_TRADES} posisi")
    log.info("═"*60)
    tg_send(
        f"💰 <b>RRL-Wealth Bot dimulai</b>\n"
        f"Pairs: {', '.join(pairs)}\n"
        f"Mode : {'TESTNET' if USE_TESTNET else '⚠️ LIVE'}"
    )

    client = create_client()
    for p in pairs:
        set_leverage(client, p)

    cycle = 0
    while True:
        cycle += 1
        try:
            open_pos = get_open_positions(client)
            balance  = get_balance(client)
            log.info(f"── Cycle #{cycle} | Balance: {balance:.2f} USDT | "
                     f"Open: {len(open_pos)}/{MAX_OPEN_TRADES} ──")

            for symbol in pairs:
                try:
                    df     = get_klines(client, symbol)
                    signal = get_signal(df)
                    last   = df.iloc[-1]
                    price  = last["close"]
                    atr    = last["atr"]

                    log.info(
                        f"[{symbol}] {price:.2f} | "
                        f"EMA20={last.ema_fast:.2f} EMA50={last.ema_slow:.2f} | "
                        f"RSI={last.rsi:.1f} | → {signal.upper()}"
                    )

                    if symbol in open_pos:
                        pos      = open_pos[symbol]
                        pos_side = "long" if float(pos["positionAmt"]) > 0 else "short"
                        upnl     = float(pos.get("unrealizedProfit", 0))
                        log.info(f"  📊 {pos_side.upper()} open | uPnL={upnl:+.2f}")

                        # Flip posisi jika sinyal berlawanan kuat
                        if (pos_side == "long"  and signal == "short") or \
                           (pos_side == "short" and signal == "long"):
                            close_trade(client, symbol, pos, "sinyal berlawanan")
                            del open_pos[symbol]
                            # Buka ke arah baru (jika slot tersedia)
                            if len(open_pos) < MAX_OPEN_TRADES:
                                open_trade(client, symbol, signal, price, atr)

                    elif signal in ("long", "short"):
                        if len(open_pos) >= MAX_OPEN_TRADES:
                            log.info(f"  ⏸ Max posisi tercapai, skip {symbol}")
                        else:
                            open_trade(client, symbol, signal, price, atr)
                            open_pos[symbol] = True  # placeholder

                except Exception as e:
                    log.error(f"[{symbol}] error: {e}")
                    time.sleep(5)

        except KeyboardInterrupt:
            log.info("Bot dihentikan (Ctrl+C)")
            tg_send("🛑 <b>Bot dihentikan manual</b>")
            break
        except Exception as e:
            log.error(f"Error: {e}", exc_info=True)
            tg_send(f"❌ <b>Error:</b> {e}")
            time.sleep(30)

        log.info(f"⏳ Scan berikutnya {SCAN_EVERY_SEC}s...\n")
        time.sleep(SCAN_EVERY_SEC)

# ══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Binance Futures Bot")
    parser.add_argument("--backtest", action="store_true",
                        help="Jalankan backtest, bukan live trading")
    parser.add_argument("--days", type=int, default=90,
                        help="Jumlah hari untuk backtest (default: 90)")
    parser.add_argument("--pairs", nargs="+", default=DEFAULT_PAIRS,
                        help="Daftar pair, contoh: --pairs BTCUSDT ETHUSDT")
    args = parser.parse_args()

    if args.backtest:
        run_backtest(args.pairs, args.days)
    else:
        run_live(args.pairs)
