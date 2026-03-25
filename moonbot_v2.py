"""
moonbot_v2.py — Polymarket 5-Min Bot (Early Entry Strategy)
============================================================
Refactor do moonbot original:

MUDANÇA PRINCIPAL: entra no INÍCIO do slot (primeiros 2 min)
em vez dos últimos 45s onde não há liquidez.

Lógica:
1. Pré-computa sinal com dados do slot anterior (últimos ~5min de candles)
2. Quando novo slot abre → entra IMEDIATAMENTE se sinal forte
3. FAK agressivo (como scanner v11) em vez de FOK
4. Preço ~50¢ no início = retorno ~100% se acertar

Sinais:
- MACD Histogram (fast=3, slow=10, signal=3)
- CVD (Cumulative Volume Delta do Binance)
- Momentum (variação % últimos 3 candles)
- RSI extremos (mean reversion)
- Lick Stink (liquidações Binance)

Risk Controls:
- Cooldown 10min por ativo após loss
- Max 2 posições abertas simultâneas
- Daily loss limit -$10
- Filtro 20-80¢ (rejeita mercados quase resolvidos)
- Slippage protection via CLOB ask real

Setup:
    pip install requests websocket-client python-dotenv colorama py-clob-client

.env:
    POLYMARKET_API_KEY=...
    POLYMARKET_SECRET=...
    POLYMARKET_PASSPHRASE=...
    WALLET_PRIVATE_KEY=...
    POLY_SAFE_ADDRESS=...
    BET_SIZE=2.0
    ASSETS=BTC,ETH,SOL
    DRY_RUN=true
"""

import os
import sys
import time
import json
import threading
import logging
import queue

# Fix encoding para Windows
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from datetime import datetime, timezone, timedelta
from collections import deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
import requests

try:
    import websocket
    HAS_WS = True
except ImportError:
    HAS_WS = False

try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init(autoreset=True)
except ImportError:
    class Fore:
        GREEN = RED = YELLOW = CYAN = WHITE = MAGENTA = BLUE = RESET = ""
    class Style:
        BRIGHT = DIM = RESET_ALL = ""

load_dotenv()

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
API_KEY          = os.getenv("POLYMARKET_API_KEY", "")
SECRET           = os.getenv("POLYMARKET_SECRET", "")
PASSPHRASE       = os.getenv("POLYMARKET_PASSPHRASE", "")
PRIVATE_KEY      = os.getenv("WALLET_PRIVATE_KEY", "")
POLY_SAFE_ADDRESS = os.getenv("POLY_SAFE_ADDRESS", "")
BET_SIZE         = float(os.getenv("BET_SIZE", "2.50"))
DRY_RUN          = os.getenv("DRY_RUN", "true").lower() != "false"
ASSETS           = [a.strip().upper() for a in os.getenv("ASSETS", "BTC,ETH,SOL").split(",")]

CLOB_BASE   = "https://clob.polymarket.com"
GAMMA_BASE  = "https://gamma-api.polymarket.com"
BINANCE_WS  = "wss://stream.binance.com:9443/stream"
BINANCE_API = "https://api.binance.com"

# Estratégia
MACD_FAST        = 3
MACD_SLOW        = 10
MACD_SIGNAL      = 3
CVD_WINDOW       = 20
MIN_CONFIDENCE   = 0.40   # confiança mínima para entrar
MIN_EDGE         = 0.05   # edge mínimo (confidence - market_price)

# ── NOVA LÓGICA DE ENTRADA ──────────────────
# Entra nos primeiros ENTRY_WINDOW_SECS do slot (quando há liquidez)
# Em vez dos últimos 45s (onde orderbook está vazio)
ENTRY_WINDOW_SECS = 120   # primeiros 2 minutos do slot
ENTRY_DELAY_SECS  = 3     # espera 3s após slot abrir (mercado precisa existir)

# ── DOUBLE DOWN ─────────────────────────────
# Se sinal ficar mais forte depois da 1a entrada, coloca mais dinheiro
MAX_ENTRIES_PER_SLOT = 2      # max entradas por slot por ativo
DOUBLEDOWN_WINDOW    = 60     # segundos após 1a entrada para considerar double down
DOUBLEDOWN_MIN_CONF  = 0.60   # confiança mínima para double down (mais alta que entrada normal)
DOUBLEDOWN_CONF_BUMP = 0.08   # sinal precisa subir pelo menos 8pp vs entrada original

# Risk Controls
LOSS_COOLDOWN_SECS = 600   # pausa 10min por ativo após streak de losses
CONSECUTIVE_LOSSES_FOR_COOLDOWN = 3  # quantos losses seguidos pra ativar cooldown
MAX_OPEN_POSITIONS = 3     # max posições abertas (subiu de 2 para 3 por causa do double down)
DAILY_LOSS_LIMIT   = 10.0  # para se P&L < -$10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("moonbot_v2.log"), logging.StreamHandler()]
)
log = logging.getLogger("moonbot_v2")

# ─────────────────────────────────────────────
# CLOB CLIENT SINGLETON
# ─────────────────────────────────────────────
_clob_client = None
_clob_lock = threading.Lock()


def get_clob_client(force_new=False):
    """Retorna CLOB client singleton. force_new=True recria (útil pré-ordem)."""
    global _clob_client
    if _clob_client is not None and not force_new:
        return _clob_client
    with _clob_lock:
        if _clob_client is not None and not force_new:
            return _clob_client
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            if not all([API_KEY, SECRET, PASSPHRASE, PRIVATE_KEY]):
                return None
            creds = ApiCreds(api_key=API_KEY, api_secret=SECRET, api_passphrase=PASSPHRASE)
            client = ClobClient(
                host=CLOB_BASE,
                key=PRIVATE_KEY,
                chain_id=137,
                signature_type=2,
                funder=POLY_SAFE_ADDRESS,
            )
            client.set_api_creds(creds)
            _clob_client = client
            log.info("CLOB client inicializado")
        except Exception as e:
            log.error(f"CLOB init error: {e}")
    return _clob_client


# ─────────────────────────────────────────────
# ESTADO GLOBAL
# ─────────────────────────────────────────────
class AssetState:
    def __init__(self, symbol):
        self.symbol            = symbol
        self.price_hist        = deque(maxlen=500)   # (ts, price, volume)
        self.klines_1m         = deque(maxlen=60)    # OHLCV 1min
        self.cvd               = 0.0
        self.cvd_hist          = deque(maxlen=CVD_WINDOW)
        self.current_mkt       = None
        self.position          = None
        self.last_trade_slot   = 0                    # slot_ts do último trade
        self.loss_cooldown_until = 0
        self.lock              = threading.Lock()
        # Sinal pré-computado do slot anterior
        self.precomputed_signal = {
            "direction": None, "confidence": 0.0,
            "details": {}, "computed_for_slot": 0
        }
        self._last_signal = {}   # para API do dashboard
        # Double-down tracking
        self.slot_entries      = 0      # quantas entradas neste slot
        self.slot_entries_slot = 0      # qual slot o contador se refere
        self.first_entry_conf  = 0.0    # confiança da 1a entrada
        self.first_entry_dir   = None   # direção da 1a entrada
        self.first_entry_ts    = 0      # timestamp da 1a entrada
        # Consecutive loss tracking
        self.consecutive_losses = 0      # losses seguidos (reseta no WIN)


assets = {s: AssetState(s) for s in ASSETS}

session_stats = {
    "trades": 0, "wins": 0, "losses": 0,
    "pnl": 0.0, "start": time.time()
}

trade_log = []
log_buffer = []

# ─────────────────────────────────────────────
# INDICADORES TÉCNICOS
# ─────────────────────────────────────────────

def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    result = sum(values[:period]) / period
    for v in values[period:]:
        result = v * k + result * (1 - k)
    return result


def compute_macd(closes, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    if len(closes) < slow + signal:
        return None, None, None
    macd_vals = []
    for i in range(slow - 1, len(closes)):
        ef = ema(list(closes)[:i + 1], fast)
        es = ema(list(closes)[:i + 1], slow)
        if ef and es:
            macd_vals.append(ef - es)
    if len(macd_vals) < signal:
        return None, None, None
    macd_line = macd_vals[-1]
    signal_line = ema(macd_vals, signal)
    histogram = macd_line - signal_line if signal_line else None
    return macd_line, signal_line, histogram


def compute_cvd(asset: AssetState):
    hist = list(asset.price_hist)
    if len(hist) < 2:
        return 0.0
    cvd = 0.0
    for i in range(1, len(hist)):
        _, p_prev, _ = hist[i - 1]
        _, p_curr, v_curr = hist[i]
        if p_curr > p_prev:
            cvd += v_curr
        elif p_curr < p_prev:
            cvd -= v_curr
    return cvd


def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ─────────────────────────────────────────────
# ANÁLISE DE SINAIS
# ─────────────────────────────────────────────

def analyze_signals(asset: AssetState) -> dict:
    """
    Combina múltiplos sinais. Retorna dict com direction, confidence, detalhes.
    """
    result = {
        "direction": None, "confidence": 0.0,
        "macd_signal": 0, "cvd_signal": 0, "momentum_signal": 0,
        "details": {}
    }

    closes = [k[4] for k in asset.klines_1m]
    if len(closes) < MACD_SLOW + MACD_SIGNAL:
        return result

    score = 0.0
    signals_count = 0

    # ── MACD Histogram ──
    macd_line, sig_line, histogram = compute_macd(closes)
    if histogram is not None:
        signals_count += 1
        prev_hists = []
        for i in range(max(0, len(closes) - 5), len(closes)):
            _, _, h = compute_macd(closes[:i + 1])
            if h is not None:
                prev_hists.append(h)

        if len(prev_hists) >= 2:
            if histogram > 0 and histogram > prev_hists[-2]:
                score += 1.0
                result["macd_signal"] = 1
                result["details"]["macd"] = f"HISTUP {histogram:.4f}"
            elif histogram < 0 and histogram < prev_hists[-2]:
                score -= 1.0
                result["macd_signal"] = -1
                result["details"]["macd"] = f"HISTDN {histogram:.4f}"
            elif histogram > 0:
                score += 0.5
                result["macd_signal"] = 1
                result["details"]["macd"] = f"HIST+ {histogram:.4f}"
            elif histogram < 0:
                score -= 0.5
                result["macd_signal"] = -1
                result["details"]["macd"] = f"HIST- {histogram:.4f}"

    # ── CVD ──
    cvd = compute_cvd(asset)
    asset.cvd_hist.append(cvd)
    if len(asset.cvd_hist) >= 5:
        signals_count += 1
        cvd_avg = sum(asset.cvd_hist) / len(asset.cvd_hist)
        if cvd > cvd_avg * 1.2:
            score += 0.8
            result["cvd_signal"] = 1
            result["details"]["cvd"] = f"CVDUP {cvd:.0f}"
        elif cvd < cvd_avg * 0.8:
            score -= 0.8
            result["cvd_signal"] = -1
            result["details"]["cvd"] = f"CVDDN {cvd:.0f}"

    # ── Momentum (últimos 3 candles) ──
    if len(closes) >= 4:
        signals_count += 1
        pct = (closes[-1] - closes[-4]) / closes[-4] if closes[-4] != 0 else 0
        if pct > 0.001:
            score += 0.6
            result["momentum_signal"] = 1
            result["details"]["momentum"] = f"MOMUP {pct * 100:.2f}%"
        elif pct < -0.001:
            score -= 0.6
            result["momentum_signal"] = -1
            result["details"]["momentum"] = f"MOMDN {pct * 100:.2f}%"

    # ── RSI extremos ──
    rsi = compute_rsi(closes)
    result["details"]["rsi"] = f"RSI {rsi:.1f}"
    if rsi < 30:
        score += 0.5
    elif rsi > 70:
        score -= 0.5

    # ── Score final ──
    if signals_count > 0:
        norm = score / signals_count
        result["confidence"] = abs(norm)
        if norm > 0.05:
            result["direction"] = "UP"
        elif norm < -0.05:
            result["direction"] = "DOWN"

    return result


# ─────────────────────────────────────────────
# LICK STINK (liquidações Binance)
# ─────────────────────────────────────────────
_liq_data = {}
_liq_lock = threading.Lock()
LICK_REV_THRESHOLD = 500_000
LICK_OG_THRESHOLD  = 300_000
LICK_WINDOW        = 300


def update_liq_data(symbol, side, usd_vol):
    now_ts = int(time.time())
    window_ts = now_ts - (now_ts % LICK_WINDOW)
    with _liq_lock:
        if symbol not in _liq_data or _liq_data[symbol]["ts"] != window_ts:
            _liq_data[symbol] = {"long_vol": 0.0, "short_vol": 0.0, "ts": window_ts}
        if side.upper() == "BUY":
            _liq_data[symbol]["long_vol"] += usd_vol
        else:
            _liq_data[symbol]["short_vol"] += usd_vol


def get_liq_snapshot(symbol):
    now_ts = int(time.time())
    window_ts = now_ts - (now_ts % LICK_WINDOW)
    with _liq_lock:
        d = _liq_data.get(symbol, {})
        if d.get("ts") != window_ts:
            return {"long_vol": 0.0, "short_vol": 0.0}
        return {"long_vol": d["long_vol"], "short_vol": d["short_vol"]}


def lick_stink_signal(asset: AssetState) -> dict:
    result = {"direction": None, "confidence": 0.0, "strategy": None, "details": {}}
    snap = get_liq_snapshot(asset.symbol)
    long_v, short_v = snap["long_vol"], snap["short_vol"]
    if long_v + short_v < 50_000:
        return result

    details = {"long_liq": f"${long_v / 1000:.0f}K", "short_liq": f"${short_v / 1000:.0f}K"}

    # Mean reversion (alta prioridade)
    if long_v >= LICK_REV_THRESHOLD:
        conf = min(0.85, 0.60 + (long_v - LICK_REV_THRESHOLD) / 2_000_000)
        details["trigger"] = f"REV long_liq=${long_v / 1000:.0f}K"
        return {"direction": "UP", "confidence": round(conf, 2), "strategy": "lick_rev", "details": details}
    if short_v >= LICK_REV_THRESHOLD:
        conf = min(0.85, 0.60 + (short_v - LICK_REV_THRESHOLD) / 2_000_000)
        details["trigger"] = f"REV short_liq=${short_v / 1000:.0f}K"
        return {"direction": "DOWN", "confidence": round(conf, 2), "strategy": "lick_rev", "details": details}

    # Continuação
    if short_v >= LICK_OG_THRESHOLD and short_v > long_v * 1.5:
        conf = min(0.70, 0.50 + (short_v - LICK_OG_THRESHOLD) / 1_000_000)
        details["trigger"] = f"CONT short_liq=${short_v / 1000:.0f}K"
        return {"direction": "UP", "confidence": round(conf, 2), "strategy": "lick_og", "details": details}
    if long_v >= LICK_OG_THRESHOLD and long_v > short_v * 1.5:
        conf = min(0.70, 0.50 + (long_v - LICK_OG_THRESHOLD) / 1_000_000)
        details["trigger"] = f"CONT long_liq=${long_v / 1000:.0f}K"
        return {"direction": "DOWN", "confidence": round(conf, 2), "strategy": "lick_og", "details": details}

    return result


# ─────────────────────────────────────────────
# POLYMARKET API
# ─────────────────────────────────────────────

SYMBOL_SLUGS = {
    "BTC": "btc-updown-5m-{ts}",
    "ETH": "eth-updown-5m-{ts}",
    "SOL": "sol-updown-5m-{ts}",
}

_market_cache = {}
_market_cache_ts = {}
MARKET_CACHE_TTL = 30


def get_current_slot_ts():
    """Retorna o timestamp do slot atual (múltiplo de 300)."""
    now_ts = int(time.time())
    return now_ts - (now_ts % 300)


def get_time_in_slot():
    """Retorna quantos segundos já passaram no slot atual."""
    now_ts = int(time.time())
    return now_ts % 300


def get_time_remaining():
    """Retorna quantos segundos faltam no slot atual."""
    return 300 - get_time_in_slot()


def fetch_clob_price(token_id: str) -> float | None:
    """Busca melhor ASK do orderbook CLOB."""
    try:
        r = requests.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=2)
        if not r.ok:
            return None
        ob = r.json()
        asks = ob.get("asks", [])
        bids = ob.get("bids", [])
        if asks:
            best_ask = float(asks[0]["price"])
            if 0.01 < best_ask < 0.99:
                return round(best_ask, 4)
        if bids and asks:
            mid = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
            if 0.01 < mid < 0.99:
                return round(mid, 4)
    except Exception:
        pass
    return None


def get_active_market(symbol: str) -> dict | None:
    """
    Busca mercado de 5min pelo slug no Polymarket.
    Tenta slot atual + próximo.
    """
    now_ts = int(time.time())
    slot_ts = get_current_slot_ts()
    cache_key = f"{symbol}-{slot_ts}"

    if (cache_key in _market_cache and
            now_ts - _market_cache_ts.get(cache_key, 0) < MARKET_CACHE_TTL):
        return _market_cache[cache_key]

    slug_tpl = SYMBOL_SLUGS.get(symbol.upper(), f"{symbol.lower()}-updown-5m-{{ts}}")
    market = None

    for offset in [0, 300]:
        wts = slot_ts + offset
        slug = slug_tpl.replace("{ts}", str(wts))

        try:
            resp = requests.get(
                f"{GAMMA_BASE}/events",
                params={"slug": slug},
                timeout=6,
            )
            if not resp.ok:
                continue

            events = resp.json()
            if not events:
                continue

            event = events[0]
            if not event.get("active") or event.get("closed"):
                continue

            mkts = event.get("markets", [])
            if not mkts:
                continue
            m = mkts[0]

            cid = m.get("conditionId", "")
            question = m.get("question", "") or event.get("title", "")
            end_date = m.get("endDate") or event.get("endDate", "")

            # Token IDs
            yes_id = no_id = None
            yes_price = no_price = 0.5

            for t in (m.get("tokens") or []):
                outcome = (t.get("outcome") or "").lower()
                tid = t.get("token_id") or t.get("tokenId", "")
                price = float(t.get("price", 0.5))
                if outcome in ("yes", "up"):
                    yes_id, yes_price = tid, price
                elif outcome in ("no", "down"):
                    no_id, no_price = tid, price

            # Fallback: clobTokenIds
            if not yes_id:
                raw = m.get("clobTokenIds", "")
                try:
                    ids = json.loads(raw) if isinstance(raw, str) else raw
                    if ids and len(ids) >= 2:
                        yes_id, no_id = ids[0], ids[1]
                except Exception:
                    pass

            # Preço real via CLOB
            if yes_id:
                clob_yes = fetch_clob_price(yes_id)
                if clob_yes:
                    yes_price = clob_yes
                    no_price = round(1.0 - clob_yes, 4)

            # Fallback: outcomePrices
            if yes_price == 0.5:
                try:
                    outcomes = json.loads(m.get("outcomes", "[]"))
                    out_prices = json.loads(m.get("outcomePrices", "[]"))
                    for i, o in enumerate(outcomes):
                        p = float(out_prices[i]) if i < len(out_prices) else 0.5
                        if o.lower() in ("yes", "up"):
                            yes_price = p
                        elif o.lower() in ("no", "down"):
                            no_price = p
                except Exception:
                    pass

            if not yes_id and DRY_RUN:
                yes_id = f"synthetic-yes-{cid or wts}"
                no_id = f"synthetic-no-{cid or wts}"

            market = {
                "id": cid,
                "question": question,
                "conditionId": cid,
                "slug": slug,
                "endDate": end_date,
                "closed": False,
                "active": True,
                "_synthetic": False,
                "yes_price": yes_price,
                "no_price": no_price,
                "yes_token_id": yes_id,
                "no_token_id": no_id,
                "slot_ts": wts,
            }
            log.info(f"[{symbol}] Mercado: {question[:60]} | YES={yes_price:.3f} NO={no_price:.3f}")
            break

        except Exception as e:
            log.debug(f"[{symbol}] Erro slug {slug}: {e}")

    # Fallback sintético (DRY_RUN)
    if not market and DRY_RUN:
        end_dt = datetime.fromtimestamp(slot_ts + 300, tz=timezone.utc).isoformat()
        names = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana"}
        market = {
            "id": f"synthetic-{symbol.lower()}-{slot_ts}",
            "question": f"Will {names.get(symbol, symbol)} go up or down?",
            "conditionId": f"synthetic-{symbol.lower()}-{slot_ts}",
            "slug": f"{symbol.lower()}-updown-5m-{slot_ts}",
            "endDate": end_dt,
            "closed": False, "active": True, "_synthetic": True,
            "yes_price": 0.5, "no_price": 0.5,
            "yes_token_id": f"synthetic-yes-{slot_ts}",
            "no_token_id": f"synthetic-no-{slot_ts}",
            "slot_ts": slot_ts,
        }

    if market:
        _market_cache[cache_key] = market
        _market_cache_ts[cache_key] = now_ts
        # Limpa cache de slots antigos
        for k in list(_market_cache.keys()):
            if str(slot_ts) not in k and str(slot_ts + 300) not in k:
                _market_cache.pop(k, None)
                _market_cache_ts.pop(k, None)

    return market


# ─────────────────────────────────────────────
# EXECUÇÃO DE ORDENS — FAK (como scanner v11)
# ─────────────────────────────────────────────

def execute_order_fak(token_id: str, amount: float, price: float, symbol: str = "") -> dict:
    """
    Executa ordem com estratégia FAK + GTC fallback (igual scanner v11).

    Args:
        token_id: ID do token (YES ou NO)
        amount: USD a gastar
        price: midpoint/ask do mercado
        symbol: para logging

    Retorna dict com success, real_price, shares, method, etc.
    """
    if DRY_RUN:
        shares = amount / price if price > 0 else amount
        log.info(f"[DRY RUN] [{symbol}] FAK BUY ${amount:.2f} @ {price * 100:.1f}c -> {shares:.2f}sh")
        return {
            "success": True, "simulated": True, "method": "DRY_FAK",
            "_real_price": price, "_real_shares": shares,
            "side": "BUY", "size": amount,
        }

    if not all([API_KEY, SECRET, PASSPHRASE, PRIVATE_KEY]):
        return {"success": False, "error": "credenciais ausentes"}

    result = {"success": False, "method": None}

    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType, OrderArgs
        from py_clob_client.order_builder.constants import BUY

        # Reset client para evitar HTTP2 stale
        global _clob_client
        _clob_client = None
        client = get_clob_client(force_new=True)
        if not client:
            return {"success": False, "error": "CLOB client indisponivel"}

        # Preço agressivo: mid × 1.30 + 2¢, cap 85¢
        aggressive_price = round(min(price * 1.30 + 0.02, 0.85), 2)

        # ── TENTATIVA 1: FAK ──
        result["method"] = "FAK"
        try:
            order = client.create_market_order(
                MarketOrderArgs(token_id=token_id, amount=amount, side=BUY, price=aggressive_price)
            )
            resp = client.post_order(order, OrderType.FAK)

            if resp and resp.get("success"):
                taking = float(resp.get("takingAmount", 0))
                making = float(resp.get("makingAmount", 0))
                if taking > 0 and making > 0:
                    real_price = round(making / taking, 4)
                    if real_price > 0.90:
                        log.warning(f"[{symbol}] FAK SLIPPAGE {real_price * 100:.1f}c > 90c")
                        return {"success": False, "error": f"slippage_{real_price:.3f}", "method": "FAK"}
                    log.info(f"[{symbol}] FAK OK: {taking:.2f}sh @ {real_price * 100:.1f}c")
                    return {
                        "success": True, "method": "FAK",
                        "_real_price": real_price, "_real_shares": taking,
                        "orderID": resp.get("orderID", ""),
                    }
                # FAK aceito sem detalhes
                return {"success": True, "method": "FAK", "_real_price": aggressive_price, "_real_shares": amount / aggressive_price}

        except Exception as fak_err:
            log.info(f"[{symbol}] FAK falhou: {str(fak_err)[:100]}")
            result["fak_error"] = str(fak_err)[:200]

        # ── TENTATIVA 2: GTC agressivo ──
        result["method"] = "GTC"
        shares = max(round(amount / aggressive_price, 2), 5.0)

        try:
            order2 = client.create_order(OrderArgs(
                token_id=token_id, price=aggressive_price, size=shares, side=BUY
            ))
            resp2 = client.post_order(order2, OrderType.GTC)

            if resp2 and resp2.get("success"):
                status = resp2.get("status", "?")
                log.info(f"[{symbol}] GTC {status}: {shares:.2f}sh @ {aggressive_price * 100:.1f}c")
                return {
                    "success": True, "method": f"GTC_{status}",
                    "_real_price": aggressive_price, "_real_shares": shares,
                    "orderID": resp2.get("orderID", ""),
                }
            else:
                return {"success": False, "error": f"GTC fail: {resp2}", "method": "GTC"}

        except Exception as gtc_err:
            log.error(f"[{symbol}] GTC exception: {str(gtc_err)[:120]}")
            return {"success": False, "error": str(gtc_err), "method": "GTC"}

    except ImportError as ie:
        return {"success": False, "error": f"import: {ie}"}
    except Exception as e:
        log.error(f"[{symbol}] Order exception: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────
# BINANCE DATA FEEDS
# ─────────────────────────────────────────────

def fetch_binance_klines(symbol, interval="1m", limit=50):
    pair = f"{symbol}USDT"
    try:
        resp = requests.get(
            f"{BINANCE_API}/api/v3/klines",
            params={"symbol": pair, "interval": interval, "limit": limit},
            timeout=5
        )
        if resp.status_code == 200:
            raw = resp.json()
            return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in raw]
    except Exception as e:
        log.warning(f"[{symbol}] Erro klines: {e}")
    return []


def start_binance_ws(asset_list):
    if not HAS_WS:
        log.warning("websocket-client nao instalado — polling REST")
        return

    agg_streams = [f"{s.lower()}usdt@aggTrade" for s in asset_list]
    liq_streams = [f"{s.lower()}usdt@forceOrder" for s in asset_list]
    streams = "/".join(agg_streams + liq_streams)
    url = f"{BINANCE_WS}?streams={streams}"

    def on_message(ws, message):
        try:
            data = json.loads(message)
            payload = data.get("data", data)
            evt = payload.get("e", "")
            symbol = payload.get("s", "").replace("USDT", "")

            if evt == "aggTrade" and symbol in assets:
                price = float(payload.get("p", 0))
                qty = float(payload.get("q", 0))
                ts = int(payload.get("T", time.time() * 1000)) // 1000
                with assets[symbol].lock:
                    assets[symbol].price_hist.append((ts, price, qty))

            elif evt == "forceOrder" and symbol in assets:
                o = payload.get("o", payload)
                side = o.get("S", "")
                qty = float(o.get("q", 0))
                price = float(o.get("p", 0))
                usd = qty * price
                if usd > 1000:
                    update_liq_data(symbol, side, usd)
        except Exception:
            pass

    def on_error(ws, error):
        log.warning(f"WS Binance erro: {error}")

    def on_close(ws, *args):
        log.info("WS Binance fechado — reconectando em 5s")
        time.sleep(5)
        start_binance_ws(asset_list)

    def on_open(ws):
        log.info(f"WS Binance conectado")

    ws = websocket.WebSocketApp(url, on_message=on_message, on_error=on_error,
                                 on_close=on_close, on_open=on_open)
    threading.Thread(target=ws.run_forever, daemon=True).start()


def poll_binance_prices():
    while True:
        for symbol, asset in assets.items():
            try:
                resp = requests.get(
                    f"{BINANCE_API}/api/v3/ticker/price",
                    params={"symbol": f"{symbol}USDT"}, timeout=3
                )
                if resp.status_code == 200:
                    price = float(resp.json().get("price", 0))
                    ts = int(time.time())
                    with asset.lock:
                        asset.price_hist.append((ts, price, 0))
            except Exception:
                pass
        time.sleep(2)


def update_klines(asset: AssetState):
    klines = fetch_binance_klines(asset.symbol, interval="1m", limit=50)
    if klines:
        with asset.lock:
            asset.klines_1m.clear()
            asset.klines_1m.extend(klines)


# ─────────────────────────────────────────────
# TRADING LOOP — EARLY ENTRY STRATEGY
# ─────────────────────────────────────────────

def trading_loop(asset: AssetState):
    """
    Loop principal — ENTRADA NO INÍCIO DO SLOT.

    Fluxo:
    1. Nos últimos ~60s do slot N-1: pré-computa sinal para slot N
    2. Quando slot N abre (time_in_slot < ENTRY_WINDOW):
       - Se sinal forte → FAK agressivo imediato
       - Compra shares ~50¢ quando preço ainda não se moveu
    3. Espera resolução no fim do slot
    4. WIN → +$1/share; LOSS → -custo

    Vantagem vs versão anterior:
    - Liquidez REAL no início do slot (orderbook não está vazio)
    - Preço ~50¢ = melhor EV
    - FAK executa de verdade (sem FOK cancelando)
    """
    log.info(f"[{asset.symbol}] Trading loop v2 iniciado (early entry)")
    sym = asset.symbol

    # Carrega candles iniciais
    update_klines(asset)
    klines_ts = time.time()

    last_slot_traded = 0   # deprecated — now using asset.slot_entries

    while True:
        try:
            now = time.time()

            # Atualiza klines a cada 30s
            if now - klines_ts > 30:
                update_klines(asset)
                klines_ts = now

            slot_ts = get_current_slot_ts()
            time_in_slot = get_time_in_slot()
            time_left = 300 - time_in_slot

            # ════════════════════════════════════════════
            # FASE 1: PRÉ-COMPUTA SINAL (últimos 60s do slot)
            # Roda quando faltam <60s e sinal ainda não foi computado para o PRÓXIMO slot
            # ════════════════════════════════════════════
            next_slot = slot_ts + 300
            if time_left <= 60 and asset.precomputed_signal["computed_for_slot"] != next_slot:
                tech = analyze_signals(asset)
                lick = lick_stink_signal(asset)

                direction = None
                confidence = 0.0
                details = {}

                # Prioridade: lick_rev > tech multi-signal > lick_og
                if lick["direction"] and lick["confidence"] >= 0.65 and lick["strategy"] == "lick_rev":
                    direction = lick["direction"]
                    confidence = lick["confidence"]
                    details = lick["details"]
                elif tech["direction"] and tech["confidence"] >= MIN_CONFIDENCE:
                    aligned = sum([
                        tech["macd_signal"] != 0,
                        tech["cvd_signal"] != 0,
                        tech["momentum_signal"] != 0,
                    ])
                    all_same = (
                        (tech["macd_signal"] > 0 and tech["direction"] == "UP") or
                        (tech["macd_signal"] < 0 and tech["direction"] == "DOWN")
                    )
                    if aligned >= 2 and all_same:
                        direction = tech["direction"]
                        confidence = tech["confidence"]
                        details = tech["details"]
                elif lick["direction"] and lick["confidence"] >= 0.60 and lick["strategy"] == "lick_og":
                    direction = lick["direction"]
                    confidence = lick["confidence"]
                    details = lick["details"]

                asset.precomputed_signal = {
                    "direction": direction,
                    "confidence": confidence,
                    "details": details,
                    "computed_for_slot": next_slot,
                }

                if direction:
                    log.info(f"[{sym}] PRE-SIGNAL para slot {next_slot}: {direction} conf={confidence:.2f} | {details}")
                    add_log("info", f"[{sym}] Pre-signal: {direction} conf={confidence:.2f}")

            # ════════════════════════════════════════════
            # FASE 2: ENTRADA NO INÍCIO DO SLOT + DOUBLE DOWN
            # 1a entrada: sinal pré-computado, primeiros segundos
            # 2a entrada (double down): sinal ao vivo ficou mais forte
            # ════════════════════════════════════════════
            in_entry_window = ENTRY_DELAY_SECS <= time_in_slot <= ENTRY_WINDOW_SECS

            # Reset entry counter when new slot starts
            if asset.slot_entries_slot != slot_ts:
                asset.slot_entries = 0
                asset.slot_entries_slot = slot_ts
                asset.first_entry_conf = 0.0
                asset.first_entry_dir = None
                asset.first_entry_ts = 0

            maxed_out = asset.slot_entries >= MAX_ENTRIES_PER_SLOT
            has_signal = (
                asset.precomputed_signal["direction"] is not None and
                asset.precomputed_signal["computed_for_slot"] == slot_ts
            )

            # Determine if this would be a first entry or double-down
            is_first_entry = asset.slot_entries == 0
            is_doubledown = (
                asset.slot_entries == 1 and
                asset.first_entry_dir is not None and
                time.time() - asset.first_entry_ts <= DOUBLEDOWN_WINDOW
            )

            can_trade = in_entry_window and not maxed_out and (
                (is_first_entry and has_signal) or is_doubledown
            )

            if can_trade:
                # For first entry, use pre-computed signal
                # For double-down, re-analyze live signals
                if is_first_entry:
                    direction = asset.precomputed_signal["direction"]
                    confidence = asset.precomputed_signal["confidence"]
                elif is_doubledown:
                    # Re-analyze signals live
                    update_klines(asset)
                    klines_ts = time.time()
                    tech = analyze_signals(asset)
                    lick = lick_stink_signal(asset)

                    # Pick strongest signal
                    direction = None
                    confidence = 0.0
                    if lick["direction"] and lick["confidence"] >= 0.65 and lick["strategy"] == "lick_rev":
                        direction, confidence = lick["direction"], lick["confidence"]
                    elif tech["direction"] and tech["confidence"] >= DOUBLEDOWN_MIN_CONF:
                        aligned = sum([tech["macd_signal"] != 0, tech["cvd_signal"] != 0, tech["momentum_signal"] != 0])
                        all_same = (
                            (tech["macd_signal"] > 0 and tech["direction"] == "UP") or
                            (tech["macd_signal"] < 0 and tech["direction"] == "DOWN")
                        )
                        if aligned >= 2 and all_same:
                            direction, confidence = tech["direction"], tech["confidence"]

                    # Double-down requirements:
                    # 1. Same direction as first entry
                    # 2. Confidence higher than first entry by DOUBLEDOWN_CONF_BUMP
                    # 3. Confidence >= DOUBLEDOWN_MIN_CONF
                    if not direction or direction != asset.first_entry_dir:
                        time.sleep(5)
                        continue
                    if confidence < DOUBLEDOWN_MIN_CONF:
                        time.sleep(5)
                        continue
                    if confidence < asset.first_entry_conf + DOUBLEDOWN_CONF_BUMP:
                        time.sleep(5)
                        continue

                    log.info(
                        f"[{sym}] DOUBLE DOWN signal: {direction} conf={confidence:.2f} "
                        f"(1st was {asset.first_entry_conf:.2f}, bump +{confidence - asset.first_entry_conf:.2f})"
                    )
                    add_log("info", f"[{sym}] DOUBLE DOWN! conf {asset.first_entry_conf:.2f} -> {confidence:.2f}")
                else:
                    time.sleep(3)
                    continue

                # ── Risk checks ──
                if time.time() < asset.loss_cooldown_until:
                    remaining = int(asset.loss_cooldown_until - time.time())
                    log.debug(f"[{sym}] Cooldown ativo: {remaining}s")
                    time.sleep(10)
                    continue

                if session_stats.get("pnl", 0) < -DAILY_LOSS_LIMIT:
                    log.info(f"[{sym}] Daily loss limit ({session_stats['pnl']:.2f})")
                    add_log("warn", f"[{sym}] Daily loss limit!")
                    time.sleep(60)
                    continue

                open_count = sum(1 for t in trade_log if t.get("status") == "open")
                if open_count >= MAX_OPEN_POSITIONS:
                    log.debug(f"[{sym}] Max posições ({open_count}/{MAX_OPEN_POSITIONS})")
                    time.sleep(5)
                    continue

                # ── Busca mercado ──
                market = get_active_market(sym)
                if not market or market.get("_synthetic"):
                    if time_in_slot < 10:
                        time.sleep(1)
                        continue
                    log.debug(f"[{sym}] Mercado indisponivel")
                    time.sleep(3)
                    continue

                with asset.lock:
                    asset.current_mkt = market

                yes_price = float(market.get("yes_price", 0.5))
                no_price = float(market.get("no_price", 0.5))
                yes_id = market.get("yes_token_id")
                no_id = market.get("no_token_id")

                if direction == "UP":
                    token_id, market_price = yes_id, yes_price
                else:
                    token_id, market_price = no_id, no_price

                if not token_id:
                    log.warning(f"[{sym}] Token ID nao encontrado")
                    time.sleep(5)
                    continue

                # ── Preço real via CLOB ──
                if not market.get("_synthetic"):
                    clob_price = fetch_clob_price(token_id)
                    if clob_price and 0.01 < clob_price < 0.99:
                        if abs(clob_price - market_price) > 0.10:
                            log.info(f"[{sym}] Preco corrigido: Gamma={market_price * 100:.1f}c CLOB={clob_price * 100:.1f}c")
                        market_price = clob_price

                # ── Filtro de preço 20-80¢ ──
                if market_price < 0.20 or market_price > 0.80:
                    log.info(f"[{sym}] Preco {market_price * 100:.1f}c fora de 20-80c")
                    asset.slot_entries = MAX_ENTRIES_PER_SLOT  # não tenta mais
                    time.sleep(3)
                    continue

                # ── Filtro EV ──
                ev = confidence - market_price
                if ev < MIN_EDGE:
                    log.debug(f"[{sym}] EV insuficiente: conf={confidence:.2f} price={market_price:.2f} ev={ev:.3f}")
                    time.sleep(5)
                    continue

                # ── Kelly sizing ──
                kelly = confidence * 0.25
                size = min(BET_SIZE * (1 + kelly), BET_SIZE * 2)
                size = max(size, 1.0)

                # Double-down gets slightly bigger size (confidence is higher)
                entry_label = "ENTRY" if is_first_entry else "DOUBLE"

                # ── Extrai janela do título ──
                mkt_question = market.get("question", "")
                mkt_window = ""
                if " - " in mkt_question:
                    mkt_window = mkt_question.split(" - ")[-1]
                elif mkt_question:
                    mkt_window = mkt_question[:50]

                log.info(
                    f"[{sym}] >>> {entry_label} {direction} | {mkt_window} | "
                    f"conf={confidence:.2f} ev={ev:.3f} | "
                    f"${size:.2f} @ {market_price * 100:.1f}c | "
                    f"time_in_slot={time_in_slot:.0f}s | entry #{asset.slot_entries + 1}"
                )
                add_log("trade",
                    f"[{sym}] {entry_label} {direction} ${size:.2f} @ {market_price * 100:.1f}c | {mkt_window}"
                )

                # ── EXECUTA FAK ──
                result = execute_order_fak(token_id, size, market_price, sym)

                real_price = result.get("_real_price", market_price)

                # Valida slippage
                if result.get("success") and real_price:
                    if real_price < 0.15 or real_price > 0.85:
                        log.warning(f"[{sym}] SLIPPAGE: {real_price * 100:.1f}c fora de 15-85c")
                        add_log("err", f"[{sym}] SLIPPAGE {real_price * 100:.1f}c")
                        asset.slot_entries = MAX_ENTRIES_PER_SLOT
                        continue

                # Registra trade
                record_trade(sym, direction, size, real_price, result, mkt_window)

                # Update entry tracking
                asset.slot_entries += 1
                if is_first_entry:
                    asset.first_entry_conf = confidence
                    asset.first_entry_dir = direction
                    asset.first_entry_ts = time.time()

                # Salva sinal para API
                asset._last_signal = {
                    "direction": direction, "confidence": confidence,
                    "details": asset.precomputed_signal.get("details", {}),
                }

                if not result.get("success"):
                    log.error(f"[{sym}] Ordem falhou: {result.get('error', '')}")
                    add_log("err", f"[{sym}] Falhou: {result.get('error', '')}")
                    if trade_log and trade_log[0].get("sym") == sym:
                        trade_log.pop(0)
                        session_stats["trades"] = max(0, session_stats["trades"] - 1)
                else:
                    method = result.get("method", "?")
                    rp = real_price * 100
                    log.info(f"[{sym}] [OK] {entry_label} {direction} via {method} @ {rp:.1f}c | {mkt_window}")
                    add_log("trade", f"[{sym}] [OK] {entry_label} {direction} via {method} @ {rp:.1f}c | {mkt_window}")

            # ── Atualiza mercado para dashboard fora da janela de entry ──
            elif time_in_slot % 10 < 1:  # a cada ~10s
                market = get_active_market(sym)
                if market:
                    with asset.lock:
                        asset.current_mkt = market

            # Polling adaptativo
            if time_left <= 70 or time_in_slot <= ENTRY_WINDOW_SECS + 10:
                time.sleep(0.5)  # rápido perto de transições
            else:
                time.sleep(3.0)  # lento no meio do slot (nada a fazer)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"[{sym}] Erro no loop: {e}", exc_info=True)
            time.sleep(5)


# ─────────────────────────────────────────────
# TRADE RECORDING & RESOLUTION
# ─────────────────────────────────────────────

def record_trade(sym, direction, size, price, result, market_window=""):
    order_id = result.get("orderID", "") if result else ""
    entry = {
        "ts": datetime.now().strftime("%H:%M:%S"),
        "sym": sym,
        "direction": direction,
        "size": size,
        "price": round(price, 4),
        "status": "open",
        "pnl": None,
        "simulated": result.get("simulated", False) if result else False,
        "market_window": market_window,
        "order_id": order_id,
        "method": result.get("method", "?") if result else "?",
    }
    trade_log.insert(0, entry)
    if len(trade_log) > 200:
        trade_log.pop()
    session_stats["trades"] += 1
    return entry


def add_log(level, msg):
    entry = {"ts": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": msg}
    log_buffer.insert(0, entry)
    if len(log_buffer) > 200:
        log_buffer.pop()


def resolve_trades():
    """Verifica trades abertos e resolve WIN/LOSS quando mercado fecha."""
    while True:
        try:
            for trade in list(trade_log):
                if trade.get("status") != "open":
                    continue

                mkt_window = trade.get("market_window", "")
                sym = trade.get("sym", "")
                direction = trade.get("direction", "")
                entry_price = float(trade.get("price", 0.5))
                size = float(trade.get("size", 2))

                if not mkt_window or not sym:
                    continue

                slug_tpl = SYMBOL_SLUGS.get(sym.upper(), f"{sym.lower()}-updown-5m-{{ts}}")

                # Parse timestamp do market_window
                trade_window_ts = None
                try:
                    ET = timezone(timedelta(hours=-4))
                    parts = mkt_window.split(", ")
                    if len(parts) >= 2:
                        time_range = parts[1].replace(" ET", "").split("-")[0]
                        year = datetime.now(ET).year
                        dt_str = f"{parts[0]} {year} {time_range}"
                        dt = datetime.strptime(dt_str, "%B %d %Y %I:%M%p").replace(tzinfo=ET)
                        trade_window_ts = int(dt.timestamp())
                        trade_window_ts = trade_window_ts - (trade_window_ts % 300)
                except Exception:
                    pass

                now_ts = int(time.time())
                slot_ts = get_current_slot_ts()

                if trade_window_ts:
                    offsets = [trade_window_ts - slot_ts]
                else:
                    offsets = [0, -300, -600, -900, -1200]

                for offset in offsets:
                    wts = slot_ts + offset
                    slug = slug_tpl.replace("{ts}", str(wts))
                    try:
                        resp = requests.get(
                            f"{GAMMA_BASE}/events",
                            params={"slug": slug}, timeout=4,
                        )
                        if not resp.ok:
                            continue
                        events = resp.json()
                        if not events:
                            continue

                        m = events[0].get("markets", [{}])[0]
                        if not (m.get("closed") or m.get("resolved")):
                            continue

                        try:
                            outcomes = json.loads(m.get("outcomes", "[]"))
                            out_prices = json.loads(m.get("outcomePrices", "[]"))
                        except Exception:
                            continue

                        winner = None
                        for i, o in enumerate(outcomes):
                            p = float(out_prices[i]) if i < len(out_prices) else 0
                            if p >= 0.99:
                                winner = o.lower()
                                break

                        if winner is None:
                            continue

                        bet_dir = direction.lower()
                        shares = size / entry_price if entry_price > 0 else size

                        if winner == bet_dir:
                            pnl = (1.0 - entry_price) * shares
                            status = "WIN"
                        else:
                            pnl = -size
                            status = "LOSS"

                        trade["status"] = status
                        trade["pnl"] = round(pnl, 4)
                        trade["winner"] = winner

                        session_stats["pnl"] += pnl
                        if status == "WIN":
                            session_stats["wins"] += 1
                            asset_obj = assets.get(sym.upper())
                            if asset_obj:
                                asset_obj.consecutive_losses = 0  # reset streak
                        else:
                            session_stats["losses"] += 1
                            asset_obj = assets.get(sym.upper())
                            if asset_obj:
                                asset_obj.consecutive_losses += 1
                                if asset_obj.consecutive_losses >= CONSECUTIVE_LOSSES_FOR_COOLDOWN:
                                    asset_obj.loss_cooldown_until = time.time() + LOSS_COOLDOWN_SECS
                                    log.info(f"[{sym}] {asset_obj.consecutive_losses} losses seguidos — cooldown {LOSS_COOLDOWN_SECS // 60}min")
                                    add_log("warn", f"[{sym}] {asset_obj.consecutive_losses}x LOSS seguidos — cooldown {LOSS_COOLDOWN_SECS // 60}min")
                                else:
                                    log.info(f"[{sym}] LOSS {asset_obj.consecutive_losses}/{CONSECUTIVE_LOSSES_FOR_COOLDOWN} — sem cooldown ainda")
                                    add_log("warn", f"[{sym}] LOSS {asset_obj.consecutive_losses}/{CONSECUTIVE_LOSSES_FOR_COOLDOWN}")

                        sign = "+" if pnl >= 0 else ""
                        log.info(f"[{sym}] {status} | {direction} | entry={entry_price * 100:.1f}c | pnl={sign}${abs(pnl):.2f} | {mkt_window}")
                        add_log("trade" if status == "WIN" else "err",
                                f"[{sym}] {status} {direction} {sign}${abs(pnl):.2f} | {mkt_window}")
                        break

                    except Exception as e:
                        log.debug(f"resolve error: {e}")

        except Exception as e:
            log.debug(f"resolve loop error: {e}")

        time.sleep(30)


# ─────────────────────────────────────────────
# HTTP API (compatível com moonbot_dashboard.html)
# ─────────────────────────────────────────────

class APIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_GET(self):
        try:
            path = self.path.split("?")[0]
            if path == "/api/status":
                self._json(self._build_status())
            elif path == "/api/trades":
                self._json({"trades": list(trade_log[:50])})
            elif path == "/api/logs":
                self._json({"logs": list(log_buffer[:80])})
            elif path == "/health":
                self._json({"ok": True})
            else:
                self.send_response(404)
                self.send_header("Connection", "close")
                self.end_headers()
        except Exception as e:
            self._json_err(str(e))

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _json_err(self, msg, code=503):
        body = json.dumps({"error": msg}).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _build_status(self):
        uptime = int(time.time() - session_stats["start"])
        asset_data = {}
        for sym, asset in assets.items():
            if not asset.lock.acquire(timeout=0.1):
                continue
            try:
                price = asset.price_hist[-1][1] if asset.price_hist else 0
                change = 0
                if len(asset.price_hist) >= 2:
                    p0 = asset.price_hist[0][1]
                    change = ((price - p0) / p0 * 100) if p0 else 0
                closes = [k[4] for k in asset.klines_1m]
                _, _, hist = compute_macd(closes) if len(closes) >= MACD_SLOW + MACD_SIGNAL else (None, None, None)
                rsi_val = compute_rsi(closes) if len(closes) >= 15 else 50
                cvd_val = compute_cvd(asset)
                mkt = asset.current_mkt
                tl = get_time_remaining() if mkt else -1
                sig = getattr(asset, "_last_signal", {})
                pre = asset.precomputed_signal
            finally:
                asset.lock.release()

            asset_data[sym] = {
                "price": round(price, 2),
                "change": round(change, 3),
                "time_left": round(tl, 1),
                "time_in_slot": get_time_in_slot(),
                "signals": {
                    "macd": round(hist, 6) if hist is not None else 0,
                    "cvd": round(cvd_val, 2),
                    "rsi": round(rsi_val, 2),
                    "momentum": round(sig.get("momentum_signal", 0), 4),
                },
                "direction": sig.get("direction") or pre.get("direction"),
                "confidence": round(sig.get("confidence", 0) or pre.get("confidence", 0), 3),
                "pre_signal": pre.get("direction"),
                "pre_confidence": round(pre.get("confidence", 0), 3),
            }

        return {
            "mode": "dry_run" if DRY_RUN else "live",
            "uptime": uptime,
            "bet_size": BET_SIZE,
            "assets": ASSETS,
            "strategy": "early_entry_v2",
            "entry_window": f"first {ENTRY_WINDOW_SECS}s",
            "stats": {
                "trades": session_stats["trades"],
                "wins": session_stats["wins"],
                "losses": session_stats["losses"],
                "pnl": round(session_stats["pnl"], 2),
            },
            "asset_data": asset_data,
        }


def start_api_server(port=8765):
    server = ThreadingHTTPServer(("localhost", port), APIHandler)
    threading.Thread(target=server.serve_forever, daemon=True, name="api-server").start()
    threading.Thread(target=resolve_trades, daemon=True, name="resolver").start()
    log.info(f"API server em http://localhost:{port}")
    add_log("info", f"API em http://localhost:{port}")


# ─────────────────────────────────────────────
# CONSOLE DASHBOARD
# ─────────────────────────────────────────────

def dashboard_loop():
    while True:
        time.sleep(30)
        try:
            uptime = int(time.time() - session_stats["start"])
            h, m = uptime // 3600, (uptime % 3600) // 60
            pnl = session_stats["pnl"]
            pnl_c = Fore.GREEN if pnl >= 0 else Fore.RED
            mode = "DRY RUN" if DRY_RUN else "LIVE"
            slot_time = get_time_in_slot()
            print()
            print(Fore.CYAN + Style.BRIGHT + f"  -- MOONBOT v2 [{mode}] (Early Entry) ----")
            print(Fore.WHITE + f"  Uptime: {h}h{m:02d}m | Trades: {session_stats['trades']}")
            print(pnl_c + f"  P&L: ${pnl:+.2f}")
            print(Fore.WHITE + f"  Slot: {slot_time}s elapsed / {300 - slot_time}s left")
            for sym, asset in assets.items():
                with asset.lock:
                    price = asset.price_hist[-1][1] if asset.price_hist else 0
                    pre = asset.precomputed_signal
                pre_str = f"{pre['direction']} {pre['confidence']:.2f}" if pre['direction'] else "---"
                print(Fore.YELLOW + f"  {sym}: ${price:,.2f} | pre-signal: {pre_str}")
            print(Fore.CYAN + Style.BRIGHT + f"  ----------------------------------------")
        except Exception:
            pass


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print(Fore.CYAN + Style.BRIGHT + """
  ╔═══════════════════════════════════════════╗
  ║   MOONBOT v2 — Early Entry Strategy       ║
  ║   Entra no INICIO do slot (primeiros 2m)  ║
  ║   FAK agressivo + sinais pre-computados   ║
  ╚═══════════════════════════════════════════╝
""")
    mode = Fore.YELLOW + "[!] DRY RUN" if DRY_RUN else Fore.RED + Style.BRIGHT + "LIVE — ordens reais!"
    print(f"  Modo: {mode}")
    print(Fore.WHITE + f"  Ativos: {', '.join(ASSETS)}")
    print(Fore.WHITE + f"  Bet size: ${BET_SIZE:.2f}")
    print(Fore.WHITE + f"  Entry: primeiros {ENTRY_WINDOW_SECS}s do slot (delay {ENTRY_DELAY_SECS}s)")
    print(Fore.WHITE + f"  Edge min: {MIN_EDGE * 100:.0f}% | Conf min: {MIN_CONFIDENCE * 100:.0f}%")
    print(Fore.WHITE + f"  Risk: cooldown {LOSS_COOLDOWN_SECS // 60}min/loss | max {MAX_OPEN_POSITIONS} pos | daily limit -${DAILY_LOSS_LIMIT}")
    print()

    if not DRY_RUN and not all([API_KEY, SECRET, PASSPHRASE, PRIVATE_KEY]):
        print(Fore.RED + "  [ERR] LIVE mode sem credenciais no .env!")
        sys.exit(1)

    # Binance data feed
    if HAS_WS:
        print(Fore.GREEN + "  [OK] WebSocket Binance...")
        start_binance_ws(ASSETS)
        time.sleep(2)
    else:
        print(Fore.YELLOW + "  [!] Sem WS — polling REST")
        threading.Thread(target=poll_binance_prices, daemon=True).start()

    # Candles iniciais
    print(Fore.WHITE + "  Carregando candles...")
    for sym, asset in assets.items():
        update_klines(asset)
        log.info(f"[{sym}] {len(asset.klines_1m)} candles")
    time.sleep(1)

    # CLOB client (live mode)
    if not DRY_RUN:
        print(Fore.WHITE + "  Inicializando CLOB client...")
        client = get_clob_client()
        print(Fore.GREEN + f"  [OK] CLOB {'pronto' if client else 'FALHOU'}")

    # API server
    start_api_server(port=8765)
    print(Fore.GREEN + "  [OK] API em http://localhost:8765")

    # Dashboard HTML
    dash = os.path.join(os.path.dirname(os.path.abspath(__file__)), "moonbot_dashboard.html")
    print(Fore.CYAN + f"  Dashboard: file:///{dash.replace(chr(92), '/')}")
    try:
        if sys.platform == "win32":
            os.startfile(dash)
    except Exception:
        pass

    # Trading threads
    print()
    threads = []
    for sym, asset in assets.items():
        t = threading.Thread(target=trading_loop, args=(asset,), daemon=True, name=f"trade-{sym}")
        t.start()
        threads.append(t)
        print(Fore.GREEN + f"  [OK] {sym} loop iniciado")

    # Console dashboard
    threading.Thread(target=dashboard_loop, daemon=True).start()

    # Mostra informação sobre timing
    slot_ts = get_current_slot_ts()
    time_in = get_time_in_slot()
    print()
    print(Fore.WHITE + f"  Slot atual: {slot_ts} | {time_in}s elapsed | {300 - time_in}s left")
    print(Fore.CYAN + "  Bot rodando. Ctrl+C para parar.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(Fore.YELLOW + "\n  Encerrando...")
        pnl = session_stats["pnl"]
        print(Fore.CYAN + Style.BRIGHT + f"\n  -- Resumo Final --")
        print(Fore.WHITE + f"  Trades: {session_stats['trades']}")
        print((Fore.GREEN if pnl >= 0 else Fore.RED) + f"  P&L: ${pnl:+.2f}")
        wr = session_stats['wins'] / max(session_stats['trades'], 1) * 100
        print(Fore.WHITE + f"  Win rate: {wr:.0f}%")
        print(Fore.CYAN + "  Ate a proxima.\n")


if __name__ == "__main__":
    main()
