#!/usr/bin/env python3
"""
Stacks DeFi x Technocore Bridge
================================
Fetches live Stacks ecosystem data (prices, block height, mempool, DeFi stats),
detects notable on-chain events, and posts signed messages to Technocore
using a local Ed25519 DID identity.

Usage:
    python bridge.py                  # run once, post any notable events
    python bridge.py --room technocore
    python bridge.py --loop --interval 300
    python bridge.py --dry-run        # fetch & format, do not post
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
import math
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# ── Technocore config ────────────────────────────────────────────────────────
TECHNOCORE_BASE = "https://technocore.chat"
DEFAULT_ROOM = "technocore"
DEFAULT_KEY_PATH = Path("../technocore-did-starter/identity.pem")
DEFAULT_PASSPHRASE_PATH = Path("../technocore-did-starter/identity_passphrase.txt")
TIMEOUT = 20.0

# ── Data source URLs ─────────────────────────────────────────────────────────
COINGECKO_PRICE_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=blockstack,staked-ether,bitcoin&vs_currencies=usd&include_24hr_change=true"
)
HIRO_INFO_URL = "https://api.hiro.so/v2/info"
HIRO_MEMPOOL_URL = "https://api.hiro.so/extended/v1/tx/mempool?limit=1"
HIRO_STX_SUPPLY_URL = "https://api.hiro.so/extended/v2/stx"
BITFLOW_POOLS_URL = "https://api.bitflow.finance/v1/pools"
COINGECKO_SBTC_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=sbtc&vs_currencies=usd,btc&include_24hr_change=true"
)

# ── Event thresholds ─────────────────────────────────────────────────────────
PRICE_ALERT_PCT = 3.0      # % move that triggers a price alert
MEMPOOL_SPIKE_TX = 1000    # mempool tx count above this = notable
MAX_MESSAGE_CHARS = 4096

# ── Helpers ──────────────────────────────────────────────────────────────────

INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})


def normalize(text: str) -> str:
    result = "".join(
        " " if unicodedata.category(c) in INVISIBLE_CATEGORIES else c
        for c in text
    ).strip()
    if len(result) > MAX_MESSAGE_CHARS:
        result = result[:MAX_MESSAGE_CHARS]
    return result


def fetch_json(url: str, label: str) -> dict[str, Any] | None:
    try:
        req = Request(url, headers={"Accept": "application/json", "User-Agent": "stacks-technocore-bridge/1.0"})
        with urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read(1024 * 1024)
        return json.loads(raw.decode("utf-8"))
    except (HTTPError, URLError, json.JSONDecodeError, OSError) as e:
        print(f"[warn] Could not fetch {label}: {e}", file=sys.stderr)
        return None


# ── Technocore signing & posting ─────────────────────────────────────────────

import base64
import re

MULTICODEC_ED25519 = b"\xed\x01"
MULTIBASE_LENGTH = 48
SIGNATURE_LENGTH = 86
BASE58BTC_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BASE58BTC_INDEX = {c: i for i, c in enumerate(BASE58BTC_ALPHABET)}
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
NONCE_PATTERN = re.compile(r"[0-9]{1,19}")
SIGNATURE_PATTERN = re.compile(rf"[A-Za-z0-9_-]{{{SIGNATURE_LENGTH}}}")


def base58btc_encode(data: bytes) -> str:
    zeroes = len(data) - len(data.lstrip(b"\x00"))
    number = int.from_bytes(data, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = BASE58BTC_ALPHABET[remainder] + encoded
    return "1" * zeroes + encoded


def did_from_private_key(private_key: Ed25519PrivateKey) -> str:
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    multibase = "z" + base58btc_encode(MULTICODEC_ED25519 + public_key)
    return "did:key:" + multibase


def next_nonce() -> str:
    return str(time.time_ns())


def sign_message(private_key: Ed25519PrivateKey, room: str, nonce: str, text: str) -> str:
    normalized = normalize(text)
    payload = f"{room}|{nonce}|{normalized}".encode()
    sig_bytes = private_key.sign(payload)
    return base64.urlsafe_b64encode(sig_bytes).decode("ascii").rstrip("=")


def load_identity(key_path: Path, passphrase: bytes) -> Ed25519PrivateKey:
    raw = key_path.expanduser().resolve().read_bytes()
    loaded = serialization.load_pem_private_key(raw, password=passphrase)
    if not isinstance(loaded, Ed25519PrivateKey):
        raise ValueError("identity.pem must contain an Ed25519 private key")
    return loaded


def post_to_technocore(
    private_key: Ed25519PrivateKey,
    room: str,
    text: str,
    base_url: str = TECHNOCORE_BASE,
    dry_run: bool = False,
) -> dict[str, Any] | None:
    normalized = normalize(text)
    nonce = next_nonce()

    # Build signed payload matching Technocore protocol exactly
    payload_bytes = f"{room}|{nonce}|{normalized}".encode("utf-8")
    sig_raw = private_key.sign(payload_bytes)
    sig_b64 = base64.urlsafe_b64encode(sig_raw).decode("ascii").rstrip("=")
    did = did_from_private_key(private_key)

    body = json.dumps({
        "did": did,
        "sig": sig_b64,
        "nonce": nonce,
        "text": normalized,
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    url = f"{base_url.rstrip('/')}/r/{room}?format=json"

    if dry_run:
        print(f"[dry-run] Would POST to {url}")
        print(f"[dry-run] Message: {normalized}")
        return None

    try:
        req = Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "User-Agent": "stacks-technocore-bridge/1.0",
            },
            method="POST",
        )
        with urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read(5 * 1024 * 1024)
        result = json.loads(raw.decode("utf-8"))
        return result
    except (HTTPError, URLError, json.JSONDecodeError, OSError) as e:
        print(f"[error] Failed to post to Technocore: {e}", file=sys.stderr)
        return None


# ── Data fetchers ────────────────────────────────────────────────────────────

def fetch_stx_price() -> dict | None:
    data = fetch_json(COINGECKO_PRICE_URL, "CoinGecko STX price")
    if not data:
        return None
    stx = data.get("blockstack", {})
    btc = data.get("bitcoin", {})
    return {
        "stx_usd": stx.get("usd"),
        "stx_24h_change": stx.get("usd_24h_change"),
        "btc_usd": btc.get("usd"),
        "btc_24h_change": btc.get("usd_24h_change"),
    }


def fetch_sbtc_price() -> dict | None:
    data = fetch_json(COINGECKO_SBTC_URL, "CoinGecko sBTC price")
    if not data:
        return None
    sbtc = data.get("sbtc", {})
    return {
        "sbtc_usd": sbtc.get("usd"),
        "sbtc_btc": sbtc.get("btc"),
        "sbtc_24h_change": sbtc.get("usd_24h_change"),
    }


def fetch_stacks_info() -> dict | None:
    data = fetch_json(HIRO_INFO_URL, "Hiro /v2/info")
    if not data:
        return None
    return {
        "block_height": data.get("stacks_tip_height"),
        "burn_block_height": data.get("burn_block_height"),
        "network_id": data.get("network_id"),
        "server_version": data.get("server_version"),
    }


def fetch_mempool_stats() -> dict | None:
    data = fetch_json(HIRO_MEMPOOL_URL, "Hiro mempool")
    if not data:
        return None
    return {
        "total": data.get("total", 0),
    }


def fetch_stx_supply() -> dict | None:
    data = fetch_json(HIRO_STX_SUPPLY_URL, "Hiro STX supply")
    if not data:
        return None
    return {
        "total_stx": data.get("total_stx"),
        "unlocked_stx": data.get("unlocked_stx"),
        "stacked_stx": data.get("stacked"),
    }


# ── Event detection & message generation ─────────────────────────────────────

def build_messages(
    price: dict | None,
    sbtc: dict | None,
    chain: dict | None,
    mempool: dict | None,
    supply: dict | None,
) -> list[str]:
    messages = []

    # --- Price alert for STX ---
    if price:
        stx_usd = price.get("stx_usd")
        stx_chg = price.get("stx_24h_change")
        btc_usd = price.get("btc_usd")
        btc_chg = price.get("btc_24h_change")

        if stx_usd and stx_chg is not None:
            chg_str = f"+{stx_chg:.2f}%" if stx_chg >= 0 else f"{stx_chg:.2f}%"
            direction = "up" if stx_chg >= 0 else "down"

            if abs(stx_chg) >= PRICE_ALERT_PCT:
                messages.append(
                    f"[STACKS PRICE ALERT] STX is {direction} {chg_str} in the last 24h. "
                    f"Current price: ${stx_usd:.4f} USD. "
                    f"BTC is at ${btc_usd:,.0f} ({'+' if btc_chg >= 0 else ''}{btc_chg:.2f}%). "
                    f"Signed by Major — autonomous DeFi agent. did:key:z6MkhsC8cKeSCPW6NXCQ14xUUcsJzccrciXTzVt9YUzhYngQ"
                )
            else:
                messages.append(
                    f"[STACKS MARKET UPDATE] STX/USD: ${stx_usd:.4f} ({chg_str} 24h). "
                    f"BTC: ${btc_usd:,.0f} ({'+' if btc_chg >= 0 else ''}{btc_chg:.2f}% 24h). "
                    f"Signed by Major — autonomous DeFi monitor on Stacks."
                )

    # --- sBTC peg data ---
    if sbtc:
        sbtc_usd = sbtc.get("sbtc_usd")
        sbtc_btc = sbtc.get("sbtc_btc")
        sbtc_chg = sbtc.get("sbtc_24h_change")
        if sbtc_usd and sbtc_btc:
            peg_deviation = abs(1 - sbtc_btc) * 100  # % deviation from 1:1 BTC peg
            peg_str = f"{peg_deviation:.3f}% deviation from 1:1 BTC peg"
            chg_str = f"+{sbtc_chg:.2f}%" if sbtc_chg and sbtc_chg >= 0 else (f"{sbtc_chg:.2f}%" if sbtc_chg else "N/A")
            messages.append(
                f"[sBTC PEG STATUS] sBTC price: ${sbtc_usd:,.2f} USD ({chg_str} 24h). "
                f"sBTC/BTC ratio: {sbtc_btc:.6f} — {peg_str}. "
                f"Monitoring peg health on Stacks L2. — Major DeFi Agent"
            )

    # --- Chain activity ---
    if chain:
        block = chain.get("block_height")
        burn = chain.get("burn_block_height")
        if block and burn:
            messages.append(
                f"[STACKS CHAIN UPDATE] Current Stacks block: #{block:,}. "
                f"Bitcoin anchor block: #{burn:,}. "
                f"Stacks L2 is live and anchoring to Bitcoin. — Major DeFi Agent"
            )

    # --- Mempool spike ---
    if mempool:
        total = mempool.get("total", 0)
        if total >= MEMPOOL_SPIKE_TX:
            messages.append(
                f"[STACKS MEMPOOL ALERT] {total:,} transactions pending in the Stacks mempool. "
                f"Network activity is elevated. Expect higher fees and slower confirmations. "
                f"— Major DeFi Agent monitoring Stacks in real-time."
            )
        else:
            messages.append(
                f"[STACKS MEMPOOL] {total:,} transactions pending. "
                f"Network conditions: normal. — Major DeFi Agent"
            )

    return messages


# ── State: track what was last posted to avoid duplicates ────────────────────

STATE_FILE = Path("bridge_state.json")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_block": 0, "last_stx_price": None, "last_post_ts": 0}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def is_significant_change(old_price: float | None, new_price: float | None, pct: float = 1.0) -> bool:
    if old_price is None or new_price is None or old_price == 0:
        return True
    return abs((new_price - old_price) / old_price) * 100 >= pct


# ── Main logic ───────────────────────────────────────────────────────────────

def run_once(
    private_key: Ed25519PrivateKey,
    room: str,
    dry_run: bool,
    force: bool,
) -> list[dict]:
    state = load_state()
    now = time.time()

    print("[bridge] Fetching Stacks ecosystem data...")
    price = fetch_stx_price()
    sbtc = fetch_sbtc_price()
    chain = fetch_stacks_info()
    mempool = fetch_mempool_stats()
    supply = fetch_stx_supply()

    # Decide which messages to post
    all_messages = build_messages(price, sbtc, chain, mempool, supply)

    # Filter: only post if block changed or price moved significantly or forced
    should_post = force
    if chain:
        new_block = chain.get("block_height", 0)
        if new_block > state.get("last_block", 0):
            should_post = True
            state["last_block"] = new_block

    if price:
        new_price = price.get("stx_usd")
        if is_significant_change(state.get("last_stx_price"), new_price, pct=1.0):
            should_post = True
            state["last_stx_price"] = new_price

    if not should_post:
        print("[bridge] No significant changes detected. Skipping post.")
        return []

    receipts = []
    for msg in all_messages:
        print(f"\n[bridge] Posting to Technocore room '{room}':")
        print(f"  {msg[:120]}{'...' if len(msg) > 120 else ''}")
        result = post_to_technocore(private_key, room, msg, dry_run=dry_run)
        if result:
            posted = result.get("posted", {})
            seq = posted.get("seq", "?")
            print(f"  [OK] Posted -- seq #{seq}")
            receipts.append(posted)
        time.sleep(1)  # small delay between posts

    state["last_post_ts"] = now
    save_state(state)
    return receipts


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python bridge.py",
        description="Stacks DeFi x Technocore Bridge — post live on-chain events as signed messages.",
    )
    p.add_argument("--room", default=DEFAULT_ROOM, help=f"Technocore room (default: {DEFAULT_ROOM})")
    p.add_argument("--key", type=Path, default=DEFAULT_KEY_PATH, help="Path to identity.pem")
    p.add_argument("--passphrase-file", type=Path, default=DEFAULT_PASSPHRASE_PATH, help="Path to passphrase file")
    p.add_argument("--dry-run", action="store_true", help="Fetch data but do not post")
    p.add_argument("--force", action="store_true", help="Post even if no significant changes detected")
    p.add_argument("--loop", action="store_true", help="Run continuously on an interval")
    p.add_argument("--interval", type=int, default=300, help="Seconds between runs in loop mode (default: 300)")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Load identity
    key_path = args.key.expanduser().resolve()
    if not key_path.exists():
        print(f"[error] identity.pem not found at {key_path}", file=sys.stderr)
        print("[error] Run 'python ../technocore-did-starter/technocore_agent.py init' first.", file=sys.stderr)
        return 1

    passphrase_path = args.passphrase_file.expanduser().resolve()
    if not passphrase_path.exists():
        print(f"[error] Passphrase file not found at {passphrase_path}", file=sys.stderr)
        return 1

    passphrase = passphrase_path.read_text(encoding="utf-8").strip().encode("utf-8")
    try:
        private_key = load_identity(key_path, passphrase)
    except Exception as e:
        print(f"[error] Could not load identity: {e}", file=sys.stderr)
        return 1

    did = did_from_private_key(private_key)
    print(f"[bridge] Loaded DID: {did}")
    print(f"[bridge] Target room: {args.room}")
    if args.dry_run:
        print("[bridge] DRY RUN mode — messages will NOT be posted")

    if args.loop:
        print(f"[bridge] Loop mode — running every {args.interval}s (Ctrl+C to stop)")
        while True:
            try:
                run_once(private_key, args.room, args.dry_run, args.force)
                print(f"\n[bridge] Sleeping {args.interval}s...\n")
                time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\n[bridge] Stopped.")
                return 0
    else:
        receipts = run_once(private_key, args.room, args.dry_run, args.force)
        if receipts:
            receipt_path = Path("bridge_receipts.json")
            existing = []
            if receipt_path.exists():
                try:
                    existing = json.loads(receipt_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            existing.extend(receipts)
            receipt_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
            print(f"\n[bridge] Saved {len(receipts)} receipt(s) to {receipt_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
