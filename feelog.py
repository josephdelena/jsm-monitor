#!/usr/bin/env python3
"""JSM fee logger: catat feeGrowthGlobal on-chain beberapa pool, maksimal sekali per jam.

Read-only. Hasil ditambahkan ke data/feegrowth.csv (satu baris per pool per jam).
Dari selisih dua baris bisa dihitung fee asli per unit likuiditas:
  fee_token = (fg_akhir - fg_awal) / 2^q * L_posisi   (q=64 Orca, q=128 EVM)

Jalankan: python feelog.py   (dipanggil workflow tiap run check; skip kalau < 55 menit)
"""
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("JSM_FEELOG", os.path.join(HERE, "data", "feegrowth.csv"))
MIN_GAP = 55 * 60
UA = {"User-Agent": "jsm-monitor/1.0", "Content-Type": "application/json"}

RPC = {
    "base": "https://mainnet.base.org",
    "avax": "https://api.avax.network/ext/bc/C/rpc",
}

POOLS = [
    # id, chain, address
    ("orca-sol-usdc", "solana", "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE"),
    ("orca-cbbtc-usdc", "solana", "HxA6SKW5qA4o12fjVgTpXdq2YnZ5Zv1s7SB4FFomsyLM"),
    ("uni-base-weth-usdc-005", "base", "0xd0b53d9277642d899df5c87a3966a349a798f224"),
    ("aero-weth-usdc-cl100", "base", "0x2a430bebd2082d8e05d56d64dede8944279bbc24"),
    ("aero-cbbtc-usdc-cl50", "base", "0x42609aa1e58c5dd7e5a012dc74118fff975f0eac"),
    ("uni-avax-usdc-005", "avax", "0xfae3f424a0a47706811521e3ee268f00cfb5c45e"),
    ("pharaoh-avax-usdc", "avax", "0xf01449c0ba930b6e2caca3def3ccbd7a3e589534"),
]

FIELDS = ["ts_utc", "pool", "chain", "fg0", "fg1", "liquidity", "sqrt_price", "tick", "q"]

SEL = {"fg0": "0xf3058399", "fg1": "0x46141319", "slot0": "0x3850c7bd", "liq": "0x1a686502"}


def post(url, body, timeout=20):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def read_orca(addr):
    d = get(f"https://api.orca.so/v2/solana/pools/{addr}")["data"]
    return {"fg0": str(int(d["feeGrowthGlobalA"])), "fg1": str(int(d["feeGrowthGlobalB"])),
            "liquidity": str(int(d["liquidity"])), "sqrt_price": str(int(d["sqrtPrice"])),
            "tick": str(int(d["tickCurrentIndex"])), "q": "64"}


def read_evm(chain, addr):
    reqs = [{"jsonrpc": "2.0", "id": i, "method": "eth_call",
             "params": [{"to": addr, "data": SEL[k]}, "latest"]}
            for i, k in enumerate(("fg0", "fg1", "slot0", "liq"))]
    try:
        res = post(RPC[chain], reqs)
        if not isinstance(res, list):
            raise RuntimeError(str(res)[:120])
    except Exception:  # noqa: BLE001  (sebagian RPC menolak batch -> satu per satu)
        res = [post(RPC[chain], r) for r in reqs]
    out = {r["id"]: r.get("result") for r in res}
    if any(not out.get(i) or out[i] == "0x" for i in range(4)):
        raise RuntimeError(f"rpc {chain}: hasil kosong")
    slot0 = out[2][2:]
    sqrt_p = int(slot0[0:64], 16)
    tick = int(slot0[64:128], 16)
    if tick >= 2 ** 255:
        tick -= 2 ** 256
    return {"fg0": str(int(out[0], 16)), "fg1": str(int(out[1], 16)),
            "liquidity": str(int(out[3], 16)), "sqrt_price": str(sqrt_p),
            "tick": str(tick), "q": "128"}


def last_ts(path):
    try:
        with open(path) as f:
            rows = f.read().strip().splitlines()
        if len(rows) < 2:
            return 0
        ts = rows[-1].split(",")[0]
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except FileNotFoundError:
        return 0


def main(force=False):
    if not force and time.time() - last_ts(OUT) < MIN_GAP:
        print("feelog: skip (belum 55 menit)")
        return 0
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows, errs = [], []
    for pid, chain, addr in POOLS:
        try:
            r = read_orca(addr) if chain == "solana" else read_evm(chain, addr)
            rows.append({"ts_utc": ts, "pool": pid, "chain": chain, **r})
        except Exception as e:  # noqa: BLE001
            errs.append(f"{pid}: {e}")
    if not rows:
        print("feelog: semua gagal\n" + "\n".join(errs))
        return 1
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    new = not os.path.exists(OUT)
    with open(OUT, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)
    print(f"feelog: {len(rows)} pool dicatat" + (f", gagal: {errs}" if errs else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(force="--force" in sys.argv))
