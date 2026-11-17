#!/usr/bin/env python3
"""JSM scanner: cari pool crypto/stablecoin yang fee-nya tinggi dibanding gerak harganya.

Read-only. Sumber: GeckoTerminal (pool + OHLCV harian pool itu sendiri),
Hyperliquid (daftar koin yang bisa di-short).

Skor = fee harian pool (rata-rata 7 hari, % TVL) / (0,25 x sigma_harian^2)
  - fee harian  = volume_7h_rata x fee_tier / TVL
  - sigma       = standar deviasi return harian harga pool 30 hari
  Skor > 1 artinya fee rata-rata LP menang dari rugi gerak harga (di level pool).
  Ini filter kasar untuk MENEMUKAN kandidat; angka asli tetap diukur dengan tes kecil.

Jalankan: python scanner.py   -> print teks ringkasan (dipakai di laporan harian)
"""
import json
import math
import os
import re
import sys
import time
import urllib.request

UA = {"User-Agent": "jsm-monitor/1.0", "Accept": "application/json"}
NETWORKS = ["solana", "base", "arbitrum", "avax", "bsc", "eth", "sui-network"]
PAGES = 2
MIN_TVL, MAX_TVL = 300_000, 15_000_000
MIN_VOL = 300_000
STABLE = re.compile(r"^(USDC|USDT|USDC\.E|USDBC|USDT0|USD₮0|USDT\.E|USDE|DAI|USDS)$", re.I)
WRAP = {"WETH": "ETH", "CBBTC": "BTC", "WBTC": "BTC", "BTCB": "BTC", "BTC.B": "BTC",
        "TBTC": "BTC", "WSOL": "SOL", "WAVAX": "AVAX", "WBNB": "BNB", "WSUI": "SUI"}
GT = "https://api.geckoterminal.com/api/v2"
SLEEP = float(os.environ.get("JSM_SCAN_SLEEP", "2.3"))


def get(url, body=None, timeout=20, retries=4):
    data = json.dumps(body).encode() if body is not None else None
    h = dict(UA)
    if data:
        h["Content-Type"] = "application/json"
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(15 if "429" in str(e) else 3)
    raise RuntimeError(f"gagal {url}: {last}")


def hedgeable():
    try:
        meta = get("https://api.hyperliquid.xyz/info", {"type": "meta"})
        return {u["name"].upper() for u in meta["universe"]}
    except Exception:  # noqa: BLE001
        return {"BTC", "ETH", "SOL", "AVAX", "BNB", "SUI", "AERO", "VIRTUAL", "ZEC", "LINK",
                "ARB", "OP", "DOGE", "XRP", "HYPE", "JUP", "WIF", "BONK", "PENDLE"}


def norm(sym, hl):
    s = sym.strip().upper()
    s = WRAP.get(s, s)
    if s not in hl and s.startswith("W") and s[1:] in hl:
        s = s[1:]
    return s


def parse_name(name):
    m = re.match(r"^(.+?) / (.+?)(?: ([\d.]+)%)?$", name)
    if not m:
        return None
    a, b, fee = m.groups()
    return a, b, (float(fee) / 100 if fee else None)


def candidates(hl):
    out = []
    for net in NETWORKS:
        for p in range(1, PAGES + 1):
            try:
                d = get(f"{GT}/networks/{net}/pools?sort=h24_volume_usd_desc&page={p}")
            except Exception:  # noqa: BLE001
                break
            for x in d.get("data", []):
                a = x["attributes"]
                pn = parse_name(a["name"])
                if not pn:
                    continue
                t0, t1, fee = pn
                if STABLE.match(t0.strip()) and not STABLE.match(t1.strip()):
                    base, quote = t1, t0
                    stable_first = True
                elif STABLE.match(t1.strip()) and not STABLE.match(t0.strip()):
                    base, quote = t0, t1
                    stable_first = False
                else:
                    continue
                coin = norm(base, hl)
                if coin not in hl:
                    continue
                tvl = float(a.get("reserve_in_usd") or 0)
                vol = float((a.get("volume_usd") or {}).get("h24") or 0)
                if not (MIN_TVL <= tvl <= MAX_TVL) or vol < MIN_VOL:
                    continue
                dex = x["relationships"]["dex"]["data"]["id"]
                if fee is None and dex == "orca":
                    try:
                        fee = get(f"https://api.orca.so/v2/solana/pools/{a['address']}")["data"]["feeRate"] / 1e6
                    except Exception:  # noqa: BLE001
                        fee = None
                if fee is None:
                    continue
                out.append({"net": net, "dex": dex,
                            "name": a["name"], "addr": a["address"], "coin": coin,
                            "fee": fee, "tvl": tvl, "vol24": vol, "stable_first": stable_first})
            time.sleep(SLEEP)
    return out


def enrich(c):
    d = get(f"{GT}/networks/{c['net']}/pools/{c['addr']}/ohlcv/day?limit=31"
            + ("&token=quote" if c.get("stable_first") else ""))
    rows = sorted(d["data"]["attributes"]["ohlcv_list"], key=lambda r: r[0])
    closes = [r[4] for r in rows if r[4]]
    vols = [r[5] for r in rows][-8:-1]  # 7 hari penuh terakhir
    if len(closes) < 15 or len(vols) < 5 or min(closes) <= 0:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    m = sum(rets) / len(rets)
    sig = math.sqrt(sum((r - m) ** 2 for r in rets) / len(rets)) * 100  # %/hari
    if sig < 0.5:  # harga stablecoin terbaca -> data salah
        return None
    vol7 = sum(vols) / len(vols)
    fee_day = vol7 * c["fee"] / c["tvl"] * 100  # % TVL per hari (level pool)
    cv = (max(vols) - min(vols)) / vol7 if vol7 else 9
    c.update({"sigma": sig, "vol7": vol7, "fee_day": fee_day,
              "score": fee_day / (0.25 * sig * sig) if sig > 0 else 0, "vol_swing": cv})
    return c


def run(top=8):
    hl = hedgeable()
    cands = candidates(hl)
    # urutkan kasar dulu (vol/TVL x fee), lalu ambil OHLCV untuk 30 teratas
    cands.sort(key=lambda c: c["vol24"] * c["fee"] / c["tvl"], reverse=True)
    res = []
    for c in cands[:30]:
        try:
            r = enrich(c)
            if r:
                res.append(r)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(SLEEP)
    res.sort(key=lambda c: c["score"], reverse=True)
    return res[:top], len(cands)


def fmt(res, n):
    lines = [f"🔎 Scanner pool (dicek {n} pool kecil-menengah, bisa di-short di Hyperliquid):",
             "skor = fee harian pool ÷ rugi gerak harga (>1 = fee menang)"]
    for c in res:
        lines.append(f"• {c['name']} [{c['dex']}, {c['net']}] skor {c['score']:.2f} | "
                     f"fee {c['fee_day']:.2f}%/hr | gerak {c['sigma']:.1f}%/hr | "
                     f"TVL ${c['tvl']/1e6:.1f}jt")
    return "\n".join(lines)


if __name__ == "__main__":
    r, n = run()
    print(fmt(r, n))
    if "--json" in sys.argv:
        print(json.dumps(r, indent=1))
