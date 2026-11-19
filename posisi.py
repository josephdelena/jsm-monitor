#!/usr/bin/env python3
"""JSM posisi: baca posisi LP milik Ef langsung dari blockchain:
Uniswap v3 (wallet EVM, Base + Avalanche) dan Orca Whirlpool (wallet Solana, lewat orca_pos.py).

Read-only (eth_call saja). Hasil: data/live_positions.json, maksimal sekali per ~55 menit.
Isi per posisi yang masih punya likuiditas: chain, id NFT, pool, harga koin (USD),
range, in/out range, nilai posisi (USD) dan fee yang belum ditarik (USD).
Dibaca oleh scheduled task Claude untuk mengupdate Sheet "Jurnal Trade JSM".

Jalankan: python posisi.py   (atau --force)
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

import orca_pos

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("JSM_LIVE", os.path.join(HERE, "data", "live_positions.json"))
MIN_GAP = 55 * 60
DEADLINE = 60  # detik
OWNERS = ["0xDab08002447CEd3778D4f7e21CE1967fFd3BC2CF",  # alamat publik wallet EVM Ef
          "0x3CC1C62b49620689De18dCC31D672e59b97f911b"]  # wallet EVM kedua (ETH Base, 19 Nov)
OWNER = OWNERS[0]
UA = {"User-Agent": "jsm-monitor/1.0", "Content-Type": "application/json"}

CHAINS = {
    "base": {"rpc": ["https://mainnet.base.org", "https://base-rpc.publicnode.com"],
             "npm": "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1",
             "factory": "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"},
    "avax": {"rpc": ["https://api.avax.network/ext/bc/C/rpc", "https://avalanche-c-chain-rpc.publicnode.com"],
             "npm": "0x655C406EBFa14EE2006250925e54ec43AD184f8B",
             "factory": "0x740b1c1de25031C31FF4fC9A62f554A55cdC1baD"},
}
STABLE = {"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC Base
          "0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e"}  # USDC Avalanche
SYMBOL = {"0x4200000000000000000000000000000000000006": "WETH",
          "0xb31f66aa3c1e785363f0875a1b74e27b85fd66c7": "WAVAX",
          "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "USDC",
          "0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e": "USDC"}
M = 2 ** 256
Q128 = 2 ** 128


# ---------- matematika murni (dites) ----------
def to_signed(v):
    return v - M if v >= 2 ** 255 else v


def fee_inside(tick, tl, tu, glob, out_lo, out_up):
    below = out_lo if tick >= tl else (glob - out_lo) % M
    above = out_up if tick < tu else (glob - out_up) % M
    return (glob - below - above) % M


def uncollected(L, inside_now, inside_last, owed):
    return owed + L * ((inside_now - inside_last) % M) // Q128


def amounts(L, sqrt_x96, tl, tu):
    s = sqrt_x96 / 2 ** 96
    sa = 1.0001 ** (tl / 2)
    sb = 1.0001 ** (tu / 2)
    sc = min(max(s, sa), sb)
    return L * (1 / sc - 1 / sb), L * (sc - sa)  # mentah (belum dibagi desimal)


def tick_price(tick, d0, d1):
    return 1.0001 ** tick * 10 ** (d0 - d1)  # token1 per token0


def value_usd(a0, a1, f0, f1, sqrt_x96, d0, d1, t0_stable):
    p = (sqrt_x96 / 2 ** 96) ** 2 * 10 ** (d0 - d1)  # token1 per token0
    h0, h1 = a0 / 10 ** d0, a1 / 10 ** d1
    g0, g1 = f0 / 10 ** d0, f1 / 10 ** d1
    if t0_stable:
        coin = 1 / p
        return h0 + h1 * coin, g0 + g1 * coin, coin
    return h0 * p + h1, g0 * p + g1, p


# ---------- rpc ----------
def pad(x):
    return format(x % M, "064x")


def pad_addr(a):
    return a.lower().replace("0x", "").rjust(64, "0")


DEAD = set()
STOP_AT = [float("inf")]


def call(chain, to, data):
    if time.time() > STOP_AT[0]:
        raise RuntimeError("lewat batas waktu")
    last = None
    for url in CHAINS[chain]["rpc"]:
        if url in DEAD:
            continue
        try:
            body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": to, "data": data}, "latest"]}
            req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=UA)
            with urllib.request.urlopen(req, timeout=8) as r:
                res = json.loads(r.read().decode())
            if not res.get("result") or res["result"] == "0x":
                raise ValueError(str(res.get("error"))[:120])
            h = res["result"][2:]
            return [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]
        except ValueError as e:
            last = e
        except Exception as e:  # noqa: BLE001  timeout/jaringan -> URL mati untuk run ini
            last = e
            DEAD.add(url)
    raise RuntimeError(f"rpc {chain}: {last}")


def read_chain(chain, closed, owner=None):
    owner = owner or OWNER
    c = CHAINS[chain]
    n = call(chain, c["npm"], "0x70a08231" + pad_addr(owner))[0]
    out, skipped = [], []
    for i in range(n):
        tid = call(chain, c["npm"], "0x2f745c59" + pad_addr(owner) + pad(i))[0]
        key = f"{chain}:{tid}"
        if key in closed:
            continue
        p = call(chain, c["npm"], "0x99fbab88" + pad(tid))
        L = p[7]
        if L == 0:
            closed.add(key)  # NFT kosong di-cache, dicek ulang sekali sehari
            continue
        t0, t1 = "0x%040x" % p[2], "0x%040x" % p[3]
        if t0.lower() not in STABLE and t1.lower() not in STABLE:
            skipped.append(f"{key}: pair tanpa USDC, dilewati")
            continue
        fee, tl, tu = p[4], to_signed(p[5]), to_signed(p[6])
        pool = "0x%040x" % call(chain, c["factory"], "0x1698ee82" + pad_addr(t0) + pad_addr(t1) + pad(fee))[0]
        s0 = call(chain, pool, "0x3850c7bd")
        sq, tick = s0[0], to_signed(s0[1])
        g0 = call(chain, pool, "0xf3058399")[0]
        g1 = call(chain, pool, "0x46141319")[0]
        lo = call(chain, pool, "0xf30dba93" + pad(tl))
        up = call(chain, pool, "0xf30dba93" + pad(tu))
        d0 = call(chain, t0, "0x313ce567")[0]
        d1 = call(chain, t1, "0x313ce567")[0]
        f0 = uncollected(L, fee_inside(tick, tl, tu, g0, lo[2], up[2]), p[8], p[10])
        f1 = uncollected(L, fee_inside(tick, tl, tu, g1, lo[3], up[3]), p[9], p[11])
        a0, a1 = amounts(L, sq, tl, tu)
        st = t0.lower() in STABLE
        val, fusd, coin_px = value_usd(a0, a1, f0, f1, sq, d0, d1, st)
        lo_px, up_px = tick_price(tl, d0, d1), tick_price(tu, d0, d1)
        if st:
            lo_px, up_px = 1 / up_px, 1 / lo_px
        coin = SYMBOL.get(t1.lower() if st else t0.lower(), "?")
        out.append({"key": f"{chain}:{tid}", "chain": chain, "dex": "Uniswap v3", "id": tid,
                    "pair": f"{coin}/USDC", "wallet": owner, "fee_tier": fee / 1e6, "pool": pool,
                    "price": round(coin_px, 6), "range": [round(lo_px, 6), round(up_px, 6)],
                    "in_range": tl <= tick < tu, "value_usd": round(val, 2), "fees_usd": round(fusd, 2),
                    "coin_amount": round((a1 / 10 ** d1) if st else (a0 / 10 ** d0), 6),
                    "usdc_amount": round((a0 / 10 ** d0) if st else (a1 / 10 ** d1), 2)})
    return out, skipped


def load_prev(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def main(force=False):
    prev = load_prev(OUT)
    if not force and time.time() - prev.get("ts_unix", 0) < MIN_GAP:
        print("posisi: skip (belum 55 menit)")
        return 0
    STOP_AT[0] = time.time() + DEADLINE
    # daftar NFT kosong di-cache, dicek ulang sekali sehari
    closed = set(prev.get("closed", [])) if time.time() - prev.get("closed_since", 0) < 86400 else set()
    closed_since = prev.get("closed_since", 0) if closed else int(time.time())
    pos, errs, notes = [], [], []
    for ch in list(CHAINS) + ["solana"]:
        try:
            if ch == "solana":
                got, skipped = orca_pos.read_orca(STOP_AT[0] + 30)
            else:
                got, skipped = [], []
                for ow in OWNERS:
                    g, s = read_chain(ch, closed, ow)
                    got += g
                    skipped += s
            pos += got
            notes += skipped
        except Exception as e:  # noqa: BLE001
            errs.append(f"{ch}: {e}")
            # chain gagal: pakai data lama, ditandai stale (jangan dianggap sudah ditutup)
            for old in prev.get("positions", []):
                if old.get("chain") == ch:
                    pos.append({**old, "stale": True})
    now = time.time()
    data = {"ts_unix": int(now),
            "ts_wib": datetime.fromtimestamp(now + 7 * 3600, timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "owner": OWNERS, "positions": pos, "errors": errs, "notes": notes,
            "closed": sorted(closed), "closed_since": closed_since}
    if errs and not pos and not prev:
        print("posisi: semua gagal", errs)
        return 1
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, OUT)
    print(f"posisi: {len(pos)} posisi" + (f", gagal: {errs}" if errs else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(force="--force" in sys.argv))
