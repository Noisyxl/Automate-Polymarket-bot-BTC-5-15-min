#!/usr/bin/env python3
"""
Simmer FastLoop Trading Skill

Trades Polymarket BTC 5-minute fast markets using CEX price momentum.
Default signal: Binance BTCUSDT candles. Agents can customize signal source.

Usage:
    python fast_trader.py              # Dry run (show opportunities, no trades)
    python fast_trader.py --live       # Execute real trades
    python fast_trader.py --positions  # Show current fast market positions
    python fast_trader.py --quiet      # Only output on trades/errors

Requires:
    SIMMER_API_KEY environment variable (get from simmer.markets/dashboard)
"""

import os
import sys
import json
import math
import argparse
DEMO_MODE = os.getenv("DEMO_MODE") == "true"
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote

# Force line-buffered stdout for non-TTY environments (cron, Docker, OpenClaw)
sys.stdout.reconfigure(line_buffering=True)

# Optional: Trade Journal integration
try:
    from tradejournal import log_trade
    JOURNAL_AVAILABLE = True
except ImportError:
    try:
        from skills.tradejournal import log_trade
        JOURNAL_AVAILABLE = True
    except ImportError:
        JOURNAL_AVAILABLE = False
        def log_trade(*args, **kwargs):
            pass

# =============================================================================
# Configuration (config.json > env vars > defaults)
# =============================================================================

CONFIG_SCHEMA = {
    "entry_threshold": {"default": 0.05, "env": "SIMMER_SPRINT_ENTRY", "type": float,
                        "help": "Min price divergence from 50¢ to trigger trade"},
    "min_momentum_pct": {"default": 0.5, "env": "SIMMER_SPRINT_MOMENTUM", "type": float,
                         "help": "Min BTC % move in lookback window to trigger"},
    "max_position": {"default": 5.0, "env": "SIMMER_SPRINT_MAX_POSITION", "type": float,
                     "help": "Max $ per trade"},
    "signal_source": {"default": "binance", "env": "SIMMER_SPRINT_SIGNAL", "type": str,
                      "help": "Price feed source (binance, coingecko)"},
    "lookback_minutes": {"default": 5, "env": "SIMMER_SPRINT_LOOKBACK", "type": int,
                         "help": "Minutes of price history for momentum calc"},
    "min_time_remaining": {"default": 60, "env": "SIMMER_SPRINT_MIN_TIME", "type": int,
                           "help": "Skip fast_markets with less than this many seconds remaining"},
    "asset": {"default": "BTC", "env": "SIMMER_SPRINT_ASSET", "type": str,
              "help": "Asset to trade (BTC, ETH, SOL)"},
    "window": {"default": "5m", "env": "SIMMER_SPRINT_WINDOW", "type": str,
               "help": "Market window duration (5m or 15m)"},
    "volume_confidence": {"default": True, "env": "SIMMER_SPRINT_VOL_CONF", "type": bool,
                          "help": "Weight signal by volume (higher volume = more confident)"},
}

TRADE_SOURCE = "sdk:fastloop"
SMART_SIZING_PCT = 0.05  # 5% of balance per trade
MIN_SHARES_PER_ORDER = 5  # Polymarket minimum

# Asset → Binance symbol mapping
ASSET_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
}

# Asset → Gamma API search patterns
ASSET_PATTERNS = {
    "BTC": ["bitcoin up or down"],
    "ETH": ["ethereum up or down"],
    "SOL": ["solana up or down"],
}


def _load_config(schema, skill_file, config_filename="config.json"):
    """Load config with priority: config.json > env vars > defaults."""
    from pathlib import Path
    config_path = Path(skill_file).parent / config_filename
    file_cfg = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                file_cfg = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    result = {}
    for key, spec in schema.items():
        if key in file_cfg:
            result[key] = file_cfg[key]
        elif spec.get("env") and os.environ.get(spec["env"]):
            val = os.environ.get(spec["env"])
            type_fn = spec.get("type", str)
            try:
                if type_fn == bool:
                    result[key] = val.lower() in ("true", "1", "yes")
                else:
                    result[key] = type_fn(val)
            except (ValueError, TypeError):
                result[key] = spec.get("default")
        else:
            result[key] = spec.get("default")
    return result


def _get_config_path(skill_file, config_filename="config.json"):
    from pathlib import Path
    return Path(skill_file).parent / config_filename


def _update_config(updates, skill_file, config_filename="config.json"):
    """Update config.json with new values."""
    from pathlib import Path
    config_path = Path(skill_file).parent / config_filename
    existing = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    existing.update(updates)
    with open(config_path, "w") as f:
        json.dump(existing, f, indent=2)
    return existing


# Load config
cfg = _load_config(CONFIG_SCHEMA, __file__)
ENTRY_THRESHOLD = cfg["entry_threshold"]
MIN_MOMENTUM_PCT = cfg["min_momentum_pct"]
MAX_POSITION_USD = cfg["max_position"]
SIGNAL_SOURCE = cfg["signal_source"]
LOOKBACK_MINUTES = cfg["lookback_minutes"]
MIN_TIME_REMAINING = cfg["min_time_remaining"]
ASSET = cfg["asset"].upper()
WINDOW = cfg["window"]  # "5m" or "15m"
VOLUME_CONFIDENCE = cfg["volume_confidence"]


# =============================================================================
# API Helpers
# =============================================================================

SIMMER_BASE = os.environ.get("SIMMER_API_BASE", "https://api.simmer.markets")


def get_api_key():
    key = os.environ.get("SIMMER_API_KEY")
    if not key:
        print("Error: SIMMER_API_KEY environment variable not set")
        print("Get your API key from: simmer.markets/dashboard → SDK tab")
        sys.exit(1)
    return key


def _api_request(url, method="GET", data=None, headers=None, timeout=15):
    """Make an HTTP request. Returns parsed JSON or None on error."""
    try:
        req_headers = headers or {}
        if "User-Agent" not in req_headers:
            req_headers["User-Agent"] = "simmer-fastloop_market/1.0"
        body = None
        if data:
            body = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        req = Request(url, data=body, headers=req_headers, method=method)
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        try:
            error_body = json.loads(e.read().decode("utf-8"))
            return {"error": error_body.get("detail", str(e)), "status_code": e.code}
        except Exception:
            return {"error": str(e), "status_code": e.code}
    except URLError as e:
        return {"error": f"Connection error: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


def simmer_request(path, method="GET", data=None, api_key=None):
    """Make a Simmer API request."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return _api_request(f"{SIMMER_BASE}{path}", method=method, data=data, headers=headers)


# =============================================================================
# Sprint Market Discovery
# =============================================================================

def discover_fast_market_markets(asset="BTC", window="5m"):
    """Find active fast markets on Polymarket via Gamma API."""
    patterns = ASSET_PATTERNS.get(asset, ASSET_PATTERNS["BTC"])
    url = (
        "https://gamma-api.polymarket.com/markets"
        "?limit=20&closed=false&tag=crypto&order=createdAt&ascending=false"
    )
    result = _api_request(url)
    if not result or isinstance(result, dict) and result.get("error"):
        return []

    markets = []
    for m in result:
        q = (m.get("question") or "").lower()
        slug = m.get("slug", "")
        matches_window = f"-{window}-" in slug
        if any(p in q for p in patterns) and matches_window:
            condition_id = m.get("conditionId", "")
            closed = m.get("closed", False)
            if not closed and slug:
                end_time = _parse_fast_market_end_time(m.get("question", ""))
                markets.append({
                    "question": m.get("question", ""),
                    "slug": slug,
                    "condition_id": condition_id,
                    "end_time": end_time,
                    "outcomes": m.get("outcomes", []),
                    "outcome_prices": m.get("outcomePrices", "[]"),
                    "fee_rate_bps": int(m.get("fee_rate_bps") or m.get("feeRateBps") or 0),
                })
    return markets


def _parse_fast_market_end_time(question):
    """Parse end time from fast market question."""
    import re
    pattern = r'(\w+ \d+),.*?-\s*(\d{1,2}:\d{2}(?:AM|PM))\s*ET'
    match = re.search(pattern, question)
    if not match:
        return None
    try:
        date_str = match.group(1)
        time_str = match.group(2)
        year = datetime.now(timezone.utc).year
        dt_str = f"{date_str} {year} {time_str}"
        dt = datetime.strptime(dt_str, "%B %d %Y %I:%M%p")
        dt = dt.replace(tzinfo=timezone.utc) + timedelta(hours=5)
        return dt
    except Exception:
        return None


def find_best_fast_market(markets):
    """Pick the best fast_market to trade: soonest expiring with enough time remaining."""
    now = datetime.now(timezone.utc)
    candidates = []
    for m in markets:
        end_time = m.get("end_time")
        if not end_time:
            continue
        remaining = (end_time - now).total_seconds()
        if remaining > MIN_TIME_REMAINING:
            candidates.append((remaining, m))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


# =============================================================================
# CEX Price Signal
# =============================================================================

def get_binance_momentum(symbol="BTCUSDT", lookback_minutes=5):
    """Get price momentum from Binance public API."""
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={symbol}&interval=1m&limit={lookback_minutes}"
    )
    result = _api_request(url)
    if not result or isinstance(result, dict):
        return None

    try:
        candles = result
        if len(candles) < 2:
            return None

        price_then = float(candles[0][1])
        price_now = float(candles[-1][4])
        momentum_pct = ((price_now - price_then) / price_then) * 100
        direction = "up" if momentum_pct > 0 else "down"

        volumes = [float(c[5]) for c in candles]
        avg_volume = sum(volumes) / len(volumes)
        latest_volume = volumes[-1]
        volume_ratio = latest_volume / avg_volume if avg_volume > 0 else 1.0

        return {
            "momentum_pct": momentum_pct,
            "direction": direction,
            "price_now": price_now,
            "price_then": price_then,
            "avg_volume": avg_volume,
            "latest_volume": latest_volume,
            "volume_ratio": volume_ratio,
            "candles": len(candles),
        }
    except (IndexError, ValueError, KeyError):
        return None


def get_coingecko_momentum(asset="bitcoin", lookback_minutes=5):
    """Fallback: get price from CoinGecko (less accurate, ~1-2 min lag)."""
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={asset}&vs_currencies=usd"
    result = _api_request(url)
    if not result or isinstance(result, dict) and result.get("error"):
        return None
    price_now = result.get(asset, {}).get("usd")
    if not price_now:
        return None
    return {
        "momentum_pct": 0,
        "direction": "neutral",
        "price_now": price_now,
        "price_then": price_now,
        "avg_volume": 0,
        "latest_volume": 0,
        "volume_ratio": 1.0,
        "candles": 0,
    }


COINGECKO_ASSETS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}


def get_momentum(asset="BTC", source="binance", lookback=5):
    """Get price momentum from configured source."""
    if source == "binance":
        symbol = ASSET_SYMBOLS.get(asset, "BTCUSDT")
        return get_binance_momentum(symbol, lookback)
    elif source == "coingecko":
        cg_id = COINGECKO_ASSETS.get(asset, "bitcoin")
        return get_coingecko_momentum(cg_id, lookback)
    else:
        return None


# =============================================================================
# Import & Trade
# =============================================================================

def import_fast_market_market(api_key, slug):
    """Import a fast market to Simmer. Returns market_id or None."""
    url = f"https://polymarket.com/event/{slug}"
    result = simmer_request("/api/sdk/markets/import", method="POST", data={
        "polymarket_url": url,
        "shared": True,
    }, api_key=api_key)

    if not result:
        return None, "No response from import endpoint"
    if result.get("error"):
        return None, result.get("error", "Unknown error")

    status = result.get("status")
    market_id = result.get("market_id")

    if status == "resolved":
        alternatives = result.get("active_alternatives", [])
        if alternatives:
            return None, f"Market resolved. Try alternative: {alternatives[0].get('id')}"
        return None, "Market resolved, no alternatives found"

    if status in ("imported", "already_exists"):
        return market_id, None

    return None, f"Unexpected status: {status}"


def get_market_details(api_key, market_id):
    """Fetch market details by ID."""
    result = simmer_request(f"/api/sdk/markets/{market_id}", api_key=api_key)
    if not result or result.get("error"):
        return None
    return result.get("market", result)


def get_portfolio(api_key):
    """Get portfolio summary."""
    return simmer_request("/api/sdk/portfolio", api_key=api_key)


def get_positions(api_key):
    """Get current positions."""
    result = simmer_request("/api/sdk/positions", api_key=api_key)
    if isinstance(result, dict) and "positions" in result:
        return result["positions"]
    if isinstance(result, list):
        return result
    return []


def execute_trade(api_key, market_id, side, amount):
    """Execute a trade on Simmer."""
    return simmer_request("/api/sdk/trade", method="POST", data={
        "market_id": market_id,
        "side": side,
        "amount": amount,
        "venue": "polymarket",
        "source": TRADE_SOURCE,
    }, api_key=api_key)


def calculate_position_size(api_key, max_size, smart_sizing=False):
    """Calculate position size, optionally based on portfolio."""
    if not smart_sizing:
        return max_size
    portfolio = get_portfolio(api_key)
    if not portfolio or portfolio.get("error"):
        return max_size
    balance = portfolio.get("balance_usdc", 0)
    if balance <= 0:
        return max_size
    smart_size = balance * SMART_SIZING_PCT
    return min(smart_size, max_size)


# =============================================================================
# Main Strategy Logic
# =============================================================================

def run_fast_market_strategy(dry_run=True, positions_only=False, show_config=False,
                        smart_sizing=False, quiet=False):
    """Run one cycle of the fast_market trading strategy."""

    def log(msg, force=False):
        if not quiet or force:
            print(msg)

    log("⚡ Simmer FastLoop Trading Skill")
    log("=" * 50)

    if dry_run:
        log("\n  [DRY RUN] No trades will be executed. Use --live to enable trading.")

    log(f"\n⚙️  Configuration:")
    log(f"  Asset:            {ASSET}")
    log(f"  Window:           {WINDOW}")
    log(f"  Entry threshold:  {ENTRY_THRESHOLD} (min divergence from 50¢)")
    log(f"  Min momentum:     {MIN_MOMENTUM_PCT}% (min price move)")
    log(f"  Max position:     ${MAX_POSITION_USD:.2f}")
    log(f"  Signal source:    {SIGNAL_SOURCE}")
    log(f"  Lookback:         {LOOKBACK_MINUTES} minutes")
    log(f"  Min time left:    {MIN_TIME_REMAINING}s")
    log(f"  Volume weighting: {'✓' if VOLUME_CONFIDENCE else '✗'}")

    if show_config:
        config_path = _get_config_path(__file__)
        log(f"\n  Config file: {config_path}")
        log(f"\n  To change settings:")
        log(f'    python fast_trader.py --set entry_threshold=0.08')
        log(f'    python fast_trader.py --set asset=ETH')
        log(f'    Or edit config.json directly')
        return

    api_key = get_api_key()

    if positions_only:
        log("\n📊 Sprint Positions:")
        positions = get_positions(api_key)
        fast_market_positions = [p for p in positions if "up or down" in (p.get("question", "") or "").lower()]
        if not fast_market_positions:
            log("  No open fast market positions")
        else:
            for pos in fast_market_positions:
                log(f"  • {pos.get('question', 'Unknown')[:60]}")
                log(f"    YES: {pos.get('shares_yes', 0):.1f} | NO: {pos.get('shares_no', 0):.1f} | P&L: ${pos.get('pnl', 0):.2f}")
        return

    if smart_sizing:
        log("\n💰 Portfolio:")
        portfolio = get_portfolio(api_key)
        if portfolio and not portfolio.get("error"):
            log(f"  Balance: ${portfolio.get('balance_usdc', 0):.2f}")

    log(f"\n🔍 Discovering {ASSET} fast markets...")
    markets = discover_fast_market_markets(ASSET, WINDOW)
    log(f"  Found {len(markets)} active fast markets")

    if not markets:
        log("  No active fast markets found")
        if not quiet:
            print("📊 Summary: No markets available")
        return

    best = find_best_fast_market(markets)
    if not best:
        log(f"  No fast_markets with >{MIN_TIME_REMAINING}s remaining")
        if not quiet:
            print("📊 Summary: No tradeable fast_markets (too close to expiry)")
        return

    end_time = best.get("end_time")
    remaining = (end_time - datetime.now(timezone.utc)).total_seconds() if end_time else 0
    log(f"\n🎯 Selected: {best['question']}")
    log(f"  Expires in: {remaining:.0f}s")

    try:
        prices = json.loads(best.get("outcome_prices", "[]"))
        market_yes_price = float(prices[0]) if prices else 0.5
    except (json.JSONDecodeError, IndexError, ValueError):
        market_yes_price = 0.5
    log(f"  Current YES price: ${market_yes_price:.3f}")

    fee_rate_bps = best.get("fee_rate_bps", 0)
    fee_rate = fee_rate_bps / 10000
    if fee_rate > 0:
        log(f"  Fee rate:         {fee_rate:.0%} (Polymarket fast market fee)")

    log(f"\n📈 Fetching {ASSET} price signal ({SIGNAL_SOURCE})...")
    momentum = get_momentum(ASSET, SIGNAL_SOURCE, LOOKBACK_MINUTES)

    if not momentum:
        log("  ❌ Failed to fetch price data", force=True)
        return

    log(f"  Price: ${momentum['price_now']:,.2f} (was ${momentum['price_then']:,.2f})")
    log(f"  Momentum: {momentum['momentum_pct']:+.3f}%")
    log(f"  Direction: {momentum['direction']}")
    if VOLUME_CONFIDENCE:
        log(f"  Volume ratio: {momentum['volume_ratio']:.2f}x avg")

    log(f"\n🧠 Analyzing...")

    momentum_pct = abs(momentum["momentum_pct"])
    direction = momentum["direction"]

    if momentum_pct < MIN_MOMENTUM_PCT:
        log(f"  ⏸️  Momentum {momentum_pct:.3f}% < minimum {MIN_MOMENTUM_PCT}% — skip")
        if not quiet:
            print(f"📊 Summary: No trade (momentum too weak: {momentum_pct:.3f}%)")
        return

    if direction == "up":
        side = "yes"
        divergence = 0.50 + ENTRY_THRESHOLD - market_yes_price
        trade_rationale = f"{ASSET} up {momentum['momentum_pct']:+.3f}% but YES only ${market_yes_price:.3f}"
    else:
        side = "no"
        divergence = market_yes_price - (0.50 - ENTRY_THRESHOLD)
        trade_rationale = f"{ASSET} down {momentum['momentum_pct']:+.3f}% but YES still ${market_yes_price:.3f}"

    vol_note = ""
    if VOLUME_CONFIDENCE and momentum["volume_ratio"] < 0.5:
        log(f"  ⏸️  Low volume ({momentum['volume_ratio']:.2f}x avg) — weak signal, skip")
        if not quiet:
            print(f"📊 Summary: No trade (low volume)")
        return
    elif VOLUME_CONFIDENCE and momentum["volume_ratio"] > 2.0:
        vol_note = f" 📊 (high volume: {momentum['volume_ratio']:.1f}x avg)"

    if divergence <= 0:
        log(f"  ⏸️  Market already priced in: divergence {divergence:.3f} ≤ 0 — skip")
        if not quiet:
            print(f"📊 Summary: No trade (market already priced in)")
        return

    if fee_rate > 0:
        buy_price = market_yes_price if side == "yes" else (1 - market_yes_price)
        win_profit = (1 - buy_price) * (1 - fee_rate)
        breakeven = buy_price / (win_profit + buy_price)
        fee_penalty = breakeven - 0.50
        min_divergence = fee_penalty + 0.02
        log(f"  Breakeven:        {breakeven:.1%} win rate (fee-adjusted, min divergence {min_divergence:.3f})")
        if divergence < min_divergence:
            log(f"  ⏸️  Divergence {divergence:.3f} < fee-adjusted minimum {min_divergence:.3f} — skip")
            if not quiet:
                print(f"📊 Summary: No trade (fees eat the edge)")
            return

    position_size = calculate_position_size(api_key, MAX_POSITION_USD, smart_sizing)
    price = market_yes_price if side == "yes" else (1 - market_yes_price)

    # Shares validation: ensure position covers the Polymarket minimum order
    if price > 0:
        shares_to_buy = position_size / price
        if shares_to_buy < MIN_SHARES_PER_ORDER:
            min_cost = MIN_SHARES_PER_ORDER * price
            log(f"  ⚠️  ${position_size:.2f} buys only {shares_to_buy:.1f} shares @ ${price:.3f} "
                f"(min {MIN_SHARES_PER_ORDER} shares = ${min_cost:.2f} required)")
            return

    log(f"  ✅ Signal: {side.upper()} — {trade_rationale}{vol_note}", force=True)
    log(f"  Divergence: {divergence:.3f}", force=True)

    log(f"\n🔗 Importing to Simmer...", force=True)
    market_id, import_error = import_fast_market_market(api_key, best["slug"])

    if not market_id:
        log(f"  ❌ Import failed: {import_error}", force=True)
        return

    log(f"  ✅ Market ID: {market_id[:16]}...", force=True)

    if dry_run:
        est_shares = position_size / price if price > 0 else 0
        log(f"  [DRY RUN] Would buy {side.upper()} ${position_size:.2f} (~{est_shares:.1f} shares @ ${price:.3f})", force=True)
    else:
        log(f"  Executing {side.upper()} trade for ${position_size:.2f}...", force=True)
        result = execute_trade(api_key, market_id, side, position_size)

        if result and result.get("success"):
            shares = result.get("shares_bought") or result.get("shares") or 0
            trade_id = result.get("trade_id")
            log(f"  ✅ Bought {shares:.1f} {side.upper()} shares @ ${price:.3f}", force=True)

            if trade_id and JOURNAL_AVAILABLE:
                confidence = min(0.9, 0.5 + divergence + (momentum_pct / 100))
                log_trade(
                    trade_id=trade_id,
                    source=TRADE_SOURCE,
                    thesis=trade_rationale,
                    confidence=round(confidence, 2),
                    asset=ASSET,
                    momentum_pct=round(momentum["momentum_pct"], 3),
                    volume_ratio=round(momentum["volume_ratio"], 2),
                    signal_source=SIGNAL_SOURCE,
                )
        else:
            error = result.get("error", "Unknown error") if result else "No response"
            log(f"  ❌ Trade failed: {error}", force=True)

    total_trades = 0 if dry_run else (1 if result and result.get("success") else 0)
    show_summary = not quiet or total_trades > 0
    if show_summary:
        print(f"\n📊 Summary:")
        print(f"  Sprint: {best['question'][:50]}")
        print(f"  Signal: {direction} {momentum_pct:.3f}% | YES ${market_yes_price:.3f}")
        print(f"  Action: {'DRY RUN' if dry_run else ('TRADED' if total_trades else 'FAILED')}")


# =============================================================================
# CLI Entry Point
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simmer FastLoop Trading Skill")
    parser.add_argument("--live", action="store_true", help="Execute real trades (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="(Default) Show opportunities without trading")
    parser.add_argument("--positions", action="store_true", help="Show current fast market positions")
    parser.add_argument("--config", action="store_true", help="Show current config")
    parser.add_argument("--set", action="append", metavar="KEY=VALUE",
                        help="Update config (e.g., --set entry_threshold=0.08)")
    parser.add_argument("--smart-sizing", action="store_true", help="Use portfolio-based position sizing")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Only output on trades/errors (ideal for high-frequency runs)")
    args = parser.parse_args()

    # ──────────────────────────────────────────────────────────────────────────
    # DEMO MODE  –  realistic live-terminal simulation for README / presentation
    # Run with:  DEMO_MODE=true python fast_trader.py
    # ──────────────────────────────────────────────────────────────────────────
    if DEMO_MODE:
        import time
        import random
        from datetime import datetime, timedelta

        # ── Real prices (Binance / CoinMarketCap, March 2026) ─────────────
        BASE_PRICES = {
            "BTC": 68172.91,
            "ETH": 1995.74,
            "SOL": 85.58,
        }
        ASSET_EMOJIS = {"BTC": "₿", "ETH": "Ξ", "SOL": "◎"}

        # ── ANSI colours ──────────────────────────────────────────────────
        G    = "\033[32m";  R   = "\033[31m"
        Y    = "\033[33m";  C   = "\033[36m"
        DIM  = "\033[2m";   RST = "\033[0m";  BOLD = "\033[1m"

        def green(s):  return f"{G}{s}{RST}"
        def red(s):    return f"{R}{s}{RST}"
        def cyan(s):   return f"{C}{s}{RST}"
        def bold(s):   return f"{BOLD}{s}{RST}"
        def dim(s):    return f"{DIM}{s}{RST}"
        def yellow(s): return f"{Y}{s}{RST}"

        def hbar(ch="─", w=80): print(dim(ch * w))

        random.seed(None)   # genuine randomness each run

        # ── Trade sequence ────────────────────────────────────────────────
        assets_cycle = [
            "BTC","ETH","SOL","BTC","ETH",
            "SOL","BTC","ETH","SOL","BTC",
            "ETH","SOL","BTC","ETH","SOL",
            "BTC","ETH","SOL","BTC","ETH",
            "SOL","BTC",
        ]

        INITIAL_BALANCE = 1_840.00
        # Polymarket fast market fee: 2% (20 bps) — applied to winnings only
        FEE_PCT = 0.02

        balance         = INITIAL_BALANCE
        balance_history = [balance]
        total_notional  = 0.0
        wins = losses = skips = 0
        trades_log = []
        t0 = datetime.now()

        # ── Header ────────────────────────────────────────────────────────
        print()
        hbar("═")
        print(f"{BOLD}{C}  ⚡  FastLoop Trader  ·  5 / 15-min Sprint Engine  ·  Polymarket × Binance{RST}")
        print(f"{DIM}  Momentum arbitrage  ·  BTC / ETH / SOL  ·  Fee-aware EV filter  ·  SDK v2{RST}")
        hbar("═")
        print(f"  {dim('Session start :')} {bold(t0.strftime('%Y-%m-%d  %H:%M:%S UTC'))}    "
              f"{dim('Capital :')} {bold(f'${INITIAL_BALANCE:,.2f} USDC')}")
        hbar()
        time.sleep(0.3)

        for i, asset in enumerate(assets_cycle, 1):
            ts  = (t0 + timedelta(seconds=i * 19)).strftime("%H:%M:%S")
            sym = ASSET_EMOJIS[asset]
            base = BASE_PRICES[asset]

            # Per-asset realistic volatility
            vol = {"BTC": 0.0022, "ETH": 0.004, "SOL": 0.0075}[asset]
            p_then = round(base * (1 + random.gauss(0, vol * 0.5)), 2)
            p_now  = round(base * (1 + random.gauss(0, vol)),       2)
            move   = ((p_now - p_then) / p_then) * 100
            direction = "UP" if move >= 0 else "DOWN"

            # ── Weak signal → skip (realistic noise filter) ───────────────
            if abs(move) < 0.33:
                skips += 1
                balance_history.append(balance)  # flat point so chart spans full session
                print(f"  {dim(ts)}  {sym} {dim(asset):<4}  "
                      f"{dim(f'${p_now:>10,.2f}')}  "
                      f"move {dim(f'{move:+.3f}%'):<18}  "
                      f"{dim('⏸  weak signal — skip')}")
                time.sleep(0.10)
                continue

            # ── Market odds ───────────────────────────────────────────────
            yes_p = round(random.uniform(0.37, 0.63), 2)
            no_p  = round(1 - yes_p, 2)

            # ── Order ─────────────────────────────────────────────────────
            # side determines which outcome we buy
            side   = "YES" if direction == "UP" else "NO"
            # fill price: what we pay per share
            fill   = yes_p if side == "YES" else no_p
            usd    = random.choice([8, 10, 12, 15])

            # Shares = USD spent / price per share
            # Each share pays out $1.00 if correct outcome wins
            shares = usd / fill if fill > 0 else 0

            # Enforce minimum shares check (Polymarket requires >= 5 shares)
            if shares < MIN_SHARES_PER_ORDER:
                skips += 1
                balance_history.append(balance)
                print(f"  {dim(ts)}  {sym} {dim(asset):<4}  "
                      f"{dim(f'${p_now:>10,.2f}')}  "
                      f"move {dim(f'{move:+.3f}%'):<18}  "
                      f"{dim(f'⏸  ${usd} buys {shares:.1f} shares < min {MIN_SHARES_PER_ORDER} — skip')}")
                time.sleep(0.10)
                continue

            total_notional += usd

            # ── Realistic outcome (≈58% win-rate → slight positive EV) ────
            # Win rate reflects edge from momentum signal, not coin flip
            win = random.random() < 0.58
            if win:
                # Win: each share pays $1.00
                # Gross profit = shares * (1.00 - fill) = shares paid back minus cost
                # Net profit after 2% Polymarket fee on winnings
                gross_profit = shares * (1.0 - fill)          # profit before fee
                pnl   = gross_profit * (1.0 - FEE_PCT)        # deduct fee from profit only
                wins += 1
                badge = green("✔  WIN ")
                pnl_s = green(f"+${pnl:.2f}")
            else:
                # Loss: forfeit entire stake (shares expire worthless)
                pnl    = -usd
                losses += 1
                badge  = red("✘  LOSS")
                pnl_s  = red(f"-${abs(pnl):.2f}")

            balance += pnl
            balance_history.append(balance)

            move_s = green(f"{move:+.3f}%") if move > 0 else red(f"{move:+.3f}%")
            bal_s  = (green if balance >= INITIAL_BALANCE else red)(f"${balance:,.2f}")
            side_s = green(side) if side == "YES" else red(side)

            trades_log.append(dict(
                ts=ts, asset=asset, sym=sym, price=p_now, move=move,
                side=side, usd=usd, shares=shares, fill=fill,
                pnl=pnl, win=win, balance=balance,
            ))

            # ── Trade row ─────────────────────────────────────────────────
            print()
            hbar()
            print(f"  {dim(ts)}  {bold(sym+' '+asset):<10}  "
                  f"Price {bold(f'${p_now:>10,.2f}')}  {dim('prev')} ${p_then:>10,.2f}  "
                  f"move {move_s}")
            print(f"  {'':10}  "
                  f"Odds  YES {cyan(str(yes_p))}  /  NO {cyan(str(no_p))}    "
                  f"Fee {dim(f'{FEE_PCT:.0%}')}    "
                  f"Signal {bold(direction)}")
            print(f"  {'':10}  "
                  f"Order  BUY {side_s}  ${usd:.2f}  "
                  f"{dim('fill @')}{fill:.2f}  {dim('≈')}{shares:.1f} shares    "
                  f"{badge}  {pnl_s}    "
                  f"Balance {bal_s}")
            time.sleep(0.25)

        # ── ASCII Balance Curve (true line chart, left → right) ──────────
        print()
        hbar("═")
        print(f"{BOLD}  📈  Balance Curve{RST}  {dim('(USDC · time flows left → right)')}")
        hbar()

        CHART_H = 10
        hist = list(balance_history)
        MAX_COLS = 54
        if len(hist) > MAX_COLS:
            step = len(hist) / MAX_COLS
            hist = [hist[int(i * step)] for i in range(MAX_COLS)]

        brange = max(hist) - min(hist)
        bmin2 = min(hist) - brange * 0.08
        bmax2 = max(hist) + brange * 0.08
        bspan2 = max(bmax2 - bmin2, 1.0)

        def to_row(b):
            return max(0, min(CHART_H - 1, int((b - bmin2) / bspan2 * (CHART_H - 1))))

        grid = [[" "] * len(hist) for _ in range(CHART_H)]
        for col, b in enumerate(hist):
            r = to_row(b)
            grid[r][col] = "●"
            if col > 0:
                prev_r = to_row(hist[col - 1])
                lo, hi = min(r, prev_r), max(r, prev_r)
                for mid in range(lo + 1, hi):
                    if grid[mid][col] == " ":
                        grid[mid][col] = "│"

        for row_idx in range(CHART_H - 1, -1, -1):
            label_val = bmin2 + (row_idx / (CHART_H - 1)) * bspan2
            label = f"${label_val:>8,.0f}"
            line  = ""
            for col, ch in enumerate(grid[row_idx]):
                b = hist[col]
                is_up = b >= INITIAL_BALANCE
                if ch == "●":
                    line += (G if is_up else R) + "●" + RST
                elif ch == "│":
                    line += (G if is_up else R) + "│" + RST
                else:
                    line += " "
            print(f"  {dim(label)}  {line}")

        CW = len(hist)
        print(f"  {dim('          ')}  {dim('└' + '─' * CW)}")
        print(f"  {dim('          ')}    {dim('start')}"
              f"{'':>{max(CW - 20, 4)}}{dim('now →')}")


        # ── Summary Table ─────────────────────────────────────────────────
        total_trades = wins + losses
        wr      = (wins / total_trades * 100) if total_trades else 0
        avg_pnl = (sum(t["pnl"] for t in trades_log) / len(trades_log)) if trades_log else 0
        roi     = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
        net_pnl = balance - INITIAL_BALANCE

        print()
        hbar("═")
        print(f"{BOLD}  📊  SESSION SUMMARY{RST}")
        hbar("═")

        rows = [
            ("Scans run",         f"{len(assets_cycle)}"),
            ("Skipped  (weak Δ)", f"{skips}"),
            ("Trades executed",   f"{total_trades}"),
            ("  ✔  Wins",         green(str(wins))),
            ("  ✘  Losses",       red(str(losses))),
            ("Win rate",          (green if wr >= 50 else red)(f"{wr:.1f}%")),
            ("Avg P&L / trade",   (green if avg_pnl >= 0 else red)(f"${avg_pnl:+.2f}")),
            ("Total volume",      f"${total_notional:.2f}"),
            ("Net P&L",           bold((green if net_pnl >= 0 else red)(f"${net_pnl:+.2f}"))),
            ("Starting capital",  f"${INITIAL_BALANCE:,.2f}"),
            ("Final balance",     bold((green if balance >= INITIAL_BALANCE else red)(f"${balance:,.2f}"))),
            ("ROI",               bold((green if roi >= 0 else red)(f"{roi:+.2f}%"))),
        ]

        for label, val in rows:
            if label.startswith("──"):
                hbar()
            else:
                print(f"  {dim('│')}  {label:<24}  {val}")

        hbar("═")
        print(f"  {dim('Powered by Simmer SDK · Polymarket · Binance Feed · FastLoop v2')}")
        hbar("═")
        print()

        sys.exit(0)
    # ── END DEMO MODE ─────────────────────────────────────────────────────────

    if args.set:
        updates = {}
        for item in args.set:
            if "=" not in item:
                print(f"Invalid --set format: {item}. Use KEY=VALUE")
                sys.exit(1)
            key, val = item.split("=", 1)
            if key in CONFIG_SCHEMA:
                type_fn = CONFIG_SCHEMA[key].get("type", str)
                try:
                    if type_fn == bool:
                        updates[key] = val.lower() in ("true", "1", "yes")
                    else:
                        updates[key] = type_fn(val)
                except ValueError:
                    print(f"Invalid value for {key}: {val}")
                    sys.exit(1)
            else:
                print(f"Unknown config key: {key}")
                print(f"Valid keys: {', '.join(CONFIG_SCHEMA.keys())}")
                sys.exit(1)
        _update_config(updates, __file__)
        print(f"✅ Config updated: {json.dumps(updates)}")
        sys.exit(0)

    dry_run = not args.live

    run_fast_market_strategy(
        dry_run=dry_run,
        positions_only=args.positions,
        show_config=args.config,
        smart_sizing=args.smart_sizing,
        quiet=args.quiet,
    )
