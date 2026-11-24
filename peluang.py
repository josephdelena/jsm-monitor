#!/usr/bin/env python3
"""JSM peluang: hitung untung BERSIH per hari tiap pool dari fee ASLI on-chain.

Read-only. Sumber:
  - data/feegrowth.csv (dicatat feelog.py tiap jam)  -> fee asli per range
  - Hyperliquid candle harian 30 hari                 -> volatilitas koin
  - Hyperliquid funding history                       -> pemasukan/biaya short

Rumus per range +-w (posisi selalu di-hedge short ~50% nilai pool):
  fee(w)      = fee asli per $ posisi per hari (dari selisih feeGrowthGlobal)
  funding     = 0,5 x funding harian (positif = short dibayar)
  rugi_keluar = (0,25w + 0,2%) per kejadian keluar range,
                rata-rata keluar tiap (w/sigma)^2 hari
  bersih(w)   = fee(w) + funding - rugi_keluar / lama_bertahan

Catatan: hadiah token Aero (mode staked) BELUM dihitung — yang dihitung fee mode unstaked.
Jalankan: python peluang.py   -> print teks ringkasan
"""
import csv
import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

import feelog

HERE = os.path.dirname(os.path.abspath(__file__))
CSV = os.environ.get("JSM_FEELOG", os.path.join(HERE, "data", "feegrowth.csv"))
RANGES = (0.035, 0.05, 0.07, 0.10)
EXIT_COST = 0.002          # biaya swap + buka/tutup short per siklus
HEDGE_FRAC = 0.5           # short ~50% nilai pool
ALERT_NET = 0.10           # %/hari -> kirim alert
MAX_STALE_H = 3            # baris terakhir lebih tua dari ini -> pool dilewati
RUN_DEADLINE = 120         # detik, batas total panggilan Hyperliquid
SKIP = {"pharaoh-avax-usdc",  # feeGrowth tidak mencerminkan fee LP (terbaca 0)
        "uni-base-weth-cbbtc-005"}  # ETH/BTC: bukan pair USD, hedge 1 koin & sigma 1 koin tidak berlaku; data mentah tetap dicatat
HL = "https://api.hyperliquid.xyz/info"
UA = {"User-Agent": "jsm-monitor/1.0", "Content-Type": "application/json"}


# ---------- matematika murni (dites) ----------
def fee_ratio_day(d0, d1, sqrt_price, q, w, hours):
    """Fee per $1 posisi range +-w per hari, dari kenaikan feeGrowthGlobal.

    d0, d1     : kenaikan feeGrowthGlobal token0/token1 (int, sudah mod 2^256)
    sqrt_price : sqrtPrice mentah (Q64 Orca, Q96 EVM)
    q          : 64 (Orca) atau 128 (EVM)  -> presisi feeGrowth
    Hasil tidak bergantung desimal token (rasio fee/nilai dalam satuan token1 mentah).
    """
    sq_bits = 64 if q == 64 else 96
    s = sqrt_price / 2 ** sq_bits
    p = s * s
    f0 = d0 / 2 ** q
    f1 = d1 / 2 ** q
    fee = f0 * p + f1
    x = 1 / s - 1 / (s * math.sqrt(1 + w))
    y = s - s * math.sqrt(1 - w)
    val = x * p + y
    if val <= 0 or hours <= 0:
        return 0.0
    return fee / val * 24 / hours


def exit_cost_day(w, sigma):
    """Rata-rata rugi keluar range per hari (sigma = volatilitas harian, pecahan)."""
    if sigma <= 0:
        return 0.0
    days = (w / sigma) ** 2
    return (0.25 * w + EXIT_COST) / days


def net_day(fee, funding_day, w, sigma):
    return fee + HEDGE_FRAC * funding_day - exit_cost_day(w, sigma)


def growth_delta(new, old, q):
    mod = 2 ** 256 if q == 128 else 2 ** 128
    return (int(new) - int(old)) % mod


def pick_base(rows, t_end, hours_back, min_hours):
    """Ambil baris paling dekat ke t_end - hours_back, minimal min_hours sebelum t_end."""
    target = t_end - hours_back * 3600
    best = None
    for r in rows:
        age = (t_end - r["t"]) / 3600
        if age < min_hours:
            continue
        if best is None or abs(r["t"] - target) < abs(best["t"] - target):
            best = r
    return best


# ---------- data ----------
def load_csv(path=CSV):
    by = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                t = datetime.strptime(r["ts_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
                by.setdefault(r["pool"], []).append({
                    "t": t, "fg0": r["fg0"], "fg1": r["fg1"],
                    "sqrt": int(r["sqrt_price"]), "q": int(r["q"])})
            except Exception:  # noqa: BLE001
                continue
    for v in by.values():
        v.sort(key=lambda r: r["t"])
    return by


def hl(body, retries=2):
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(HL, data=json.dumps(body).encode(), headers=UA)
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3)
    raise RuntimeError(f"hyperliquid: {last}")


def sigma_day(coin, days=30):
    now = int(time.time() * 1000)
    c = hl({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1d",
                                              "startTime": now - (days + 2) * 86400000, "endTime": now}})
    closes = [float(x["c"]) for x in c][:-1][-(days + 1):]  # buang candle hari ini (belum selesai)
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    if len(rets) < 10:
        raise RuntimeError(f"candle {coin} kurang")
    m = sum(rets) / len(rets)
    return math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))


def funding_day(coin, days):
    now = int(time.time() * 1000)
    h = hl({"type": "fundingHistory", "coin": coin, "startTime": now - days * 86400000})
    rates = [float(x["fundingRate"]) for x in h]
    return sum(rates) / len(rates) * 24 if rates else 0.0  # pecahan per hari


# ---------- hitung ----------
def evaluate(by, sig, fund7, fund1, now=None):
    out = []
    coins = {pid: coin for pid, _c, _a, coin in feelog.POOLS}
    for pid, rows in by.items():
        if pid in SKIP or pid not in coins or len(rows) < 2:
            continue
        coin = coins[pid]
        if coin not in sig:
            continue
        end = rows[-1]
        if now is not None and now - end["t"] > MAX_STALE_H * 3600:
            continue  # data pool ini sudah basi (feelog gagal), jangan dilaporkan
        for label, back, min_h, fund in (("7 hari", 168, 120, fund7), ("24 jam", 24, 18, fund1)):
            base = pick_base(rows[:-1], end["t"], back, min_h)
            if base:
                break
        if not base:
            continue
        hours = (end["t"] - base["t"]) / 3600
        label = f"{hours / 24:.1f} hari" if hours >= 36 else f"{hours:.0f} jam"
        strong = hours >= 120
        d0 = growth_delta(end["fg0"], base["fg0"], end["q"])
        d1 = growth_delta(end["fg1"], base["fg1"], end["q"])
        best = None
        for w in RANGES:
            fee = fee_ratio_day(d0, d1, end["sqrt"], end["q"], w, hours)
            n = net_day(fee, fund.get(coin, 0.0), w, sig[coin])
            r = {"w": w, "fee": fee, "net": n}
            if best is None or n > best["net"]:
                best = r
        out.append({"pool": pid, "coin": coin, "window": label, "hours": hours, "strong": strong,
                    "sigma": sig[coin], "funding": fund.get(coin, 0.0), **best})
    out.sort(key=lambda r: r["net"], reverse=True)
    return out


def run():
    by = load_csv()
    coins = sorted({c for _p, _ch, _a, c in feelog.POOLS})
    sig, f7, f1, errs = {}, {}, {}, []
    t0 = time.time()
    for c in coins:
        if time.time() - t0 > RUN_DEADLINE:
            errs.append(f"{c}: lewat batas waktu")
            continue
        try:
            s_ = sigma_day(c)
            a = funding_day(c, 7)
            b = funding_day(c, 1)
        except Exception as e:  # noqa: BLE001  (koin dilewati utuh, bukan funding diam-diam 0)
            errs.append(f"{c}: {e}")
            continue
        sig[c], f7[c], f1[c] = s_, a, b
        time.sleep(0.5)
    return evaluate(by, sig, f7, f1, now=time.time()), errs


def pct(x, d=2):
    return f"{x * 100:+.{d}f}%"


def fmt(res, errs=(), top=6):
    lines = ["💡 Peluang pool (fee ASLI on-chain, sudah dikurangi rugi keluar range & biaya hedge):"]
    if not res:
        lines.append("• belum ada data (feelog butuh ≥18 jam)")
    for r in res[:top]:
        lines.append(f"• {r['pool']} ±{r['w']*100:g}%: bersih {pct(r['net'])}/hari "
                     f"(~{pct(r['net']*30, 1)}/bln) | fee {r['fee']*100:.2f}% | "
                     f"funding short {pct(HEDGE_FRAC*r['funding'], 3)} | gerak {r['sigma']*100:.1f}%/hr "
                     f"[data {r['window']}]")
    if errs:
        lines.append("(gagal: " + "; ".join(errs) + ")")
    lines.append("Catatan: fee dihitung seolah posisi selalu in range (batas atas, range sempit cenderung dilebihkan). "
                 "Belum termasuk hadiah AERO (mode staked).")
    return "\n".join(lines)


def alerts(res):
    """Pool yang bersihnya ≥ ALERT_NET %/hari. Data 24 jam ditandai (belum kuat)."""
    return [r for r in res if r["net"] * 100 >= ALERT_NET]


def fmt_alert(hot):
    lines = ["🚨 PELUANG JSM — pool dengan untung bersih di atas batas:"]
    for r in hot:
        warn = "" if r["strong"] else f" ⚠️ baru data {r['window']}, belum kuat"
        lines.append(f"• {r['pool']} ±{r['w']*100:g}%: bersih {pct(r['net'])}/hari "
                     f"(~{pct(r['net']*30, 1)}/bln){warn}")
    lines.append("Cek angka di aplikasi dulu sebelum buka posisi.")
    return "\n".join(lines)


if __name__ == "__main__":
    r, e = run()
    print(fmt(r, e))
    if alerts(r):
        print()
        print(fmt_alert(alerts(r)))
    if "--json" in sys.argv:
        print(json.dumps(r, indent=1))
