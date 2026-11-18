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
    "base": ["https://mainnet.base.org", "https://base-rpc.publicnode.com"],
    "avax": ["https://api.avax.network/ext/bc/C/rpc", "https://avalanche-c-chain-rpc.publicnode.com"],
    "arbitrum": ["https://arb1.arbitrum.io/rpc", "https://arbitrum-one-rpc.publicnode.com"],
    "eth": ["https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com"],
}

POOLS = [
    # id, chain, address, koin (untuk hedge & volatilitas)
    # 7 pertama = daftar lama, id jangan diubah (riwayat CSV nyambung)
    ("orca-sol-usdc", "solana", "Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE", "SOL"),
    ("orca-cbbtc-usdc", "solana", "HxA6SKW5qA4o12fjVgTpXdq2YnZ5Zv1s7SB4FFomsyLM", "BTC"),
    ("uni-base-weth-usdc-005", "base", "0xd0b53d9277642d899df5c87a3966a349a798f224", "ETH"),
    ("aero-weth-usdc-cl100", "base", "0x2a430bebd2082d8e05d56d64dede8944279bbc24", "ETH"),
    ("aero-cbbtc-usdc-cl50", "base", "0x42609aa1e58c5dd7e5a012dc74118fff975f0eac", "BTC"),
    ("uni-avax-usdc-005", "avax", "0xfae3f424a0a47706811521e3ee268f00cfb5c45e", "AVAX"),
    ("pharaoh-avax-usdc", "avax", "0xf01449c0ba930b6e2caca3def3ccbd7a3e589534", "AVAX"),
    # tambahan 18 Nov 2026 (hasil scan 34 pool)
    ("orca-zec-usdc", "solana", "GTHKH8s82ZR8GTSFZ1dUu6wfdxhy59wpMShxzG5zjiPm", "ZEC"),
    ("uni-base-weth-usdc-03", "base", "0x6c561b446416e1a00e8e93e221854d6ea4171372", "ETH"),
    ("uni-base-weth-usdc-001", "base", "0xb4cb800910b228ed3d0834cf79d697127bbb00e5", "ETH"),
    ("uni-base-cbbtc-usdc-005", "base", "0xfbb6eed8e7aa03b138556eedaf5d271a5e1e43ef", "BTC"),
    ("pcs-base-weth-usdc-001", "base", "0x72ab388e2e2f6facef59e3c3fa2c4e29011c2d38", "ETH"),
    ("pcs-base-cbbtc-usdc-001", "base", "0xb94b22332abf5f89877a14cc88f2abc48c34b3df", "BTC"),
    ("aero-weth-usdc-004", "base", "0x4a7af46bc8abc00048c6c7920a1488daf568366c", "ETH"),
    ("aero-weth-usdc-005", "base", "0xb2cc224c1c9fee385f8ad6a55b4d94e92359dc59", "ETH"),
    ("aero-cbbtc-usdc-0178", "base", "0x784e539f15ca2d19449a6311c5dab6cf37d8f1ba", "BTC"),
    ("aero-aero-usdc", "base", "0x422df9aa896e883c9cdcbad309c0785b7364a1cf", "AERO"),
    ("uni-arb-weth-usdc-005", "arbitrum", "0xc6962004f452be9203591991d15f6b388e09e8d0", "ETH"),
    ("uni-arb-weth-usdc-005b", "arbitrum", "0xc31e54c7a869b9fcbecc14363cf510d1c41fa443", "ETH"),
    ("uni-arb-weth-usdc-03", "arbitrum", "0xc473e2aee3441bf9240be85eb122abb059a3b57c", "ETH"),
    ("uni-arb-wbtc-usdc-005", "arbitrum", "0x0e4831319a50228b9e450861297ab92dee15b44f", "BTC"),
    ("uni-arb-wbtc-usdt-005", "arbitrum", "0x5969efdde3cf5c0d9a88ae51e47d721096a97203", "BTC"),
    ("uni-arb-weth-usdt-005", "arbitrum", "0x641c00a822e8b671738d32a431a4fb6074e5c79d", "ETH"),
    ("uni-avax-btcb-usdc-005", "avax", "0x2e587b9e7aa638d7eb7db5fe7447513bc4d0d28b", "BTC"),
    ("uni-eth-weth-usdc-005", "eth", "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640", "ETH"),
    ("uni-eth-weth-usdc-03", "eth", "0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8", "ETH"),
    ("uni-eth-weth-usdt-001", "eth", "0xc7bbec68d12a0d1830360f8ec58fa599ba1b0e9b", "ETH"),
    ("uni-eth-wbtc-usdt-005", "eth", "0x56534741cd8b152df6d48adf7ac51f75169a83b2", "BTC"),
]

FIELDS = ["ts_utc", "pool", "chain", "fg0", "fg1", "liquidity", "sqrt_price", "tick", "q"]

SEL = {"fg0": "0xf3058399", "fg1": "0x46141319", "slot0": "0x3850c7bd", "liq": "0x1a686502"}


def post(url, body, timeout=8):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


DEAD = set()          # URL RPC yang gagal/timeout -> tidak dicoba lagi di run ini
DEADLINE = 150        # detik; lewat dari ini berhenti dan simpan yang sudah dapat


def read_orca(addr):
    d = get(f"https://api.orca.so/v2/solana/pools/{addr}")["data"]
    return {"fg0": str(int(d["feeGrowthGlobalA"])), "fg1": str(int(d["feeGrowthGlobalB"])),
            "liquidity": str(int(d["liquidity"])), "sqrt_price": str(int(d["sqrtPrice"])),
            "tick": str(int(d["tickCurrentIndex"])), "q": "64"}


def read_evm(chain, addr):
    reqs = [{"jsonrpc": "2.0", "id": i, "method": "eth_call",
             "params": [{"to": addr, "data": SEL[k]}, "latest"]}
            for i, k in enumerate(("fg0", "fg1", "slot0", "liq"))]
    out, last = None, None
    for url in RPC[chain]:
        if url in DEAD:
            continue
        try:
            res = post(url, reqs)
            if not isinstance(res, list):
                # RPC ini menolak batch (bukan timeout) -> coba satu per satu
                res = [post(url, r) for r in reqs]
            got = {r["id"]: r.get("result") for r in res}
            if any(not got.get(i) or got[i] == "0x" for i in range(4)):
                raise ValueError("hasil kosong")
            out = got
            break
        except ValueError as e:  # pool tidak cocok di RPC ini; URL tetap hidup
            last = e
        except Exception as e:  # noqa: BLE001  timeout / jaringan -> tandai mati
            last = e
            DEAD.add(url)
    if out is None:
        raise RuntimeError(f"rpc {chain}: {last}")
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
    t0 = time.time()
    for pid, chain, addr, _coin in POOLS:
        if time.time() - t0 > DEADLINE:
            errs.append(f"{pid}: lewat batas waktu {DEADLINE}s, dilewati")
            continue
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
