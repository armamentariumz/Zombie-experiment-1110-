
"""
Solana memecoin screener -> Discord alerts. Alert-only: no wallet, no trading.
Runs once per invocation (built for GitHub Actions on a cron schedule).
State (dedupe + paper-trade results) is saved in seen.json.
"""
import json
import os
import time

import requests

WEBHOOK = os.environ["https://discord.com/api/webhooks/1557565253745643581/XSFZT274GLAlEq2JW_UF958S3zikv4O2vD3Ga7zPyg46OAYybd78_QEXhnEA-CseNg08"]
STATE_FILE = "seen.json"

# ---------- Filters: tune these ----------
MIN_LIQ_USD = 10_000        # minimum liquidity
MIN_VOL_H1 = 5_000          # minimum 1h volume
MAX_VOL_LIQ_RATIO = 25      # volume/liquidity above this looks like wash trading
MIN_AGE_MIN = 10            # skip brand-new pools (rugs and snipers)
MAX_AGE_MIN = 360           # skip older than 6h
MIN_BUYS_H1 = 30            # organic activity
MIN_BUY_RATIO = 0.45        # buys / (buys + sells) over 1h
MAX_FDV = 5_000_000
USE_RUGCHECK = True         # free RugCheck lookup (authorities, holders, LP)
MAX_ALERTS_PER_RUN = 5

DEX = "https://api.dexscreener.com"
HEADERS = {"User-Agent": "Mozilla/5.0 memecoin-screener"}


def get_json(url, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                return r.json()
            print(f"HTTP {r.status_code} for {url}")
        except Exception as e:
            print(f"Request error: {e}")
        time.sleep(2 * (i + 1))
    return None


def send_alert(text):
    try:
        r = requests.post(WEBHOOK, json={"content": text[:1900]}, timeout=15)
        if r.status_code >= 300:
            print(f"Discord error {r.status_code}: {r.text}")
    except Exception as e:
        print(f"Discord error: {e}")


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("seen", {})
    s.setdefault("alerts", {})
    return s


def save_state(state):
    now = time.time()
    # forget old seen tokens after 3 days
    state["seen"] = {m: t for m, t in state["seen"].items() if now - t < 3 * 86400}
    # keep the most recent 500 tracked alerts
    items = sorted(state["alerts"].items(), key=lambda kv: kv[1]["t"], reverse=True)[:500]
    state["alerts"] = dict(items)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def candidate_mints():
    """Latest token profiles + boosted tokens, Solana only."""
    mints = []
    for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
        data = get_json(DEX + path)
        if not isinstance(data, list):
            continue
        for item in data:
            if item.get("chainId") == "solana" and item.get("tokenAddress"):
                mints.append(item["tokenAddress"])
    return list(dict.fromkeys(mints))  # dedupe, keep order


def fetch_pairs(mints):
    """Return {mint: best pair by liquidity}. DexScreener allows 30 per call."""
    best = {}
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        data = get_json(f"{DEX}/latest/dex/tokens/{','.join(chunk)}")
        if not data:
            continue
        for p in data.get("pairs") or []:
            if p.get("chainId") != "solana":
                continue
            mint = p["baseToken"]["address"]
            liq = (p.get("liquidity") or {}).get("usd") or 0
            cur = best.get(mint)
            if cur is None or liq > ((cur.get("liquidity") or {}).get("usd") or 0):
                best[mint] = p
    return best


def check_pair(p):
    """Hard filters. Returns (passed, reasons_failed)."""
    fails = []
    liq = (p.get("liquidity") or {}).get("usd") or 0
    vol1 = (p.get("volume") or {}).get("h1") or 0
    tx = (p.get("txns") or {}).get("h1") or {}
    buys, sells = tx.get("buys", 0), tx.get("sells", 0)
    fdv = p.get("fdv") or 0
    created = p.get("pairCreatedAt")
    age_min = (time.time() * 1000 - created) / 60000 if created else None

    if liq < MIN_LIQ_USD:
        fails.append("low liquidity")
    if vol1 < MIN_VOL_H1:
        fails.append("low volume")
    if liq and vol1 / liq > MAX_VOL_LIQ_RATIO:
        fails.append("volume/liq looks wash-traded")
    if age_min is None or age_min < MIN_AGE_MIN or age_min > MAX_AGE_MIN:
        fails.append("age out of range")
    if buys < MIN_BUYS_H1:
        fails.append("few buys")
    if buys + sells and buys / (buys + sells) < MIN_BUY_RATIO:
        fails.append("sell pressure")
    if fdv and fdv > MAX_FDV:
        fails.append("fdv too high")
    return (not fails), fails


def rugcheck(mint):
    """Returns (ok, notes). Fails open if the API is down, so check the notes."""
    data = get_json(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary", tries=2)
    if not data:
        return True, ["rugcheck unavailable"]
    risks = data.get("risks") or []
    danger = [r.get("name", "?") for r in risks if r.get("level") == "danger"]
    warn = [r.get("name", "?") for r in risks if r.get("level") == "warn"]
    if danger:
        return False, [f"DANGER: {', '.join(danger)}"]
    return True, ([f"warn: {', '.join(warn)}"] if warn else ["no flags"])


def format_alert(p, notes):
    b = p["baseToken"]
    liq = (p.get("liquidity") or {}).get("usd") or 0
    vol1 = (p.get("volume") or {}).get("h1") or 0
    tx = (p.get("txns") or {}).get("h1") or {}
    pc = p.get("priceChange") or {}
    age = (time.time() * 1000 - p["pairCreatedAt"]) / 60000
    return (
        f"**{b.get('symbol')}** ({b.get('name')})\n"
        f"Price: ${p.get('priceUsd')}  |  FDV: ${p.get('fdv', 0):,.0f}\n"
        f"Liq: ${liq:,.0f}  |  Vol 1h: ${vol1:,.0f}\n"
        f"1h txns: {tx.get('buys', 0)} buys / {tx.get('sells', 0)} sells\n"
        f"Change: 5m {pc.get('m5', 0)}%  1h {pc.get('h1', 0)}%\n"
        f"Age: {age:.0f} min\n"
        f"RugCheck: {'; '.join(notes)}\n"
        f"<{p.get('url')}>\n"
        f"`{b['address']}`"
    )


def update_outcomes(state):
    """Paper-trade log: record price ~1h and ~24h after each alert."""
    now = time.time()
    pending = []
    for mint, a in state["alerts"].items():
        age = now - a["t"]
        if ("p1h" not in a and age >= 3600) or ("p24h" not in a and age >= 86400):
            pending.append(mint)
    if not pending:
        return
    pairs = fetch_pairs(pending)
    for mint in pending:
        a = state["alerts"][mint]
        age = now - a["t"]
        p = pairs.get(mint)
        # if the pair vanished, count it as a total loss (likely rugged)
        price = float(p["priceUsd"]) if p and p.get("priceUsd") else 0.0
        if "p1h" not in a and age >= 3600:
            a["p1h"] = price
        if "p24h" not in a and age >= 86400:
            a["p24h"] = price


def print_stats(state):
    for key, label in (("p1h", "1h"), ("p24h", "24h")):
        rows = [a for a in state["alerts"].values() if key in a and a["entry"] > 0]
        if not rows:
            continue
        ch = [(a[key] / a["entry"] - 1) * 100 for a in rows]
        wins = sum(1 for c in ch if c > 0)
        print(f"{label}: {len(rows)} alerts, {wins} up ({wins / len(rows):.0%}), "
              f"median {sorted(ch)[len(ch) // 2]:.1f}%, avg {sum(ch) / len(ch):.1f}%")


def main():
    state = load_state()
    update_outcomes(state)

    mints = [m for m in candidate_mints() if m not in state["seen"]]
    print(f"{len(mints)} new candidates")
    pairs = fetch_pairs(mints)

    sent = 0
    now = time.time()
    for mint in mints:
        p = pairs.get(mint)
        if not p:
            continue
        ok, fails = check_pair(p)
        if not ok:
            # only mark as seen if it's out of range for good, otherwise
            # it may qualify on a later run (e.g. still too young)
            if "age out of range" in fails and (p.get("pairCreatedAt") or 0) < (now - MAX_AGE_MIN * 60) * 1000:
                state["seen"][mint] = now
            continue
        notes = ["rugcheck off"]
        if USE_RUGCHECK:
            ok, notes = rugcheck(mint)
            if not ok:
                state["seen"][mint] = now
                print(f"{p['baseToken'].get('symbol')} rejected: {notes}")
                continue
        send_alert(format_alert(p, notes))
        state["seen"][mint] = now
        state["alerts"][mint] = {
            "t": now,
            "symbol": p["baseToken"].get("symbol"),
            "entry": float(p.get("priceUsd") or 0),
        }
        sent += 1
        if sent >= MAX_ALERTS_PER_RUN:
            break

    print(f"Sent {sent} alerts")
    print_stats(state)
    save_state(state)


if __name__ == "__main__":
    main()
