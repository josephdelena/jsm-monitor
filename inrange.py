"""JSM inrange: hitung berapa lama tiap posisi benar-benar in range, dan fee per jam in range.

Dipanggil dari posisi.py tiap jam. Harga diambil dari candle 5 menit Hyperliquid (BTC/ETH/SOL),
lalu disesuaikan ke harga pool (selisih kecil antara harga Hyperliquid dan harga di pool).
Candle dihitung in range kalau harga penutupnya ada di dalam range posisi.

State disimpan di dalam data/live_positions.json (field "ir" per posisi), jadi tidak ada file baru.
Fee per jam = (fee belum ditarik sekarang - fee saat mulai dihitung) / jam in range.
Catatan: kalau fee di-harvest/collect di tengah jalan, angka fee ikut turun (belum ditangani).
"""
import calendar
import json
import time
import urllib.request

HL = "https://api.hyperliquid.xyz/info"
UA = {"User-Agent": "jsm-monitor/1.0", "Content-Type": "application/json"}
STEP = 300  # detik per candle
MAX_BACK = 7 * 86400
COIN = {"cbBTC": "BTC", "BTC.b": "BTC", "WBTC": "BTC", "WETH": "ETH", "ETH": "ETH", "SOL": "SOL"}

# Jam buka posisi yang sudah jalan sebelum fitur ini ada (WIB, 19 Nov 2026).
# Kunci = key posisi, atau "pair@chain" untuk posisi yang key-nya belum diketahui.
WIB = 7 * 3600
SEED = {
    "orca:9peBGwopHZ8qKuBAg9t18PukjQoqgKrK8zNDGudXD8Bq": "2026-11-19 14:21",
    "base:6612348": "2026-11-19 14:28",
    "avax:90698": "2026-11-19 14:37",
    "base:6613009": "2026-11-19 18:35",
    "orca:5MW5YW3hjDqYT3aHydi78mvoCpmZfY2zsqeu7pwXXREX": "2026-11-19 18:58",
    "SOL/USDC@solana": "2026-11-19 19:09",
}


def wib_ts(s):
    return calendar.timegm(time.strptime(s, "%Y-%m-%d %H:%M")) - WIB


def seed_for(p):
    s = SEED.get(p["key"]) or SEED.get(f"{p['pair']}@{p['chain']}")
    return wib_ts(s) if s else None


def coin_of(pair):
    return COIN.get(pair.split("/")[0])


def candles(coin, start, end, timeout=10):
    body = {"type": "candleSnapshot", "req": {"coin": coin, "interval": "5m",
                                              "startTime": start * 1000, "endTime": end * 1000}}
    req = urllib.request.Request(HL, data=json.dumps(body).encode(), headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return [(int(c["t"]) // 1000, float(c["c"])) for c in json.loads(r.read().decode())]


def count(cs, since, upto, lo, hi, ratio):
    """Jumlah detik in range dari candle yang mulai >= since dan selesai <= upto.
    Kembalikan (detik_in, detik_total, akhir_candle_terakhir_yang_dihitung)."""
    tin = ttot = 0
    last = since
    for t, c in cs:
        if t < since or t + STEP > upto:
            continue
        ttot += STEP
        if lo <= c * ratio < hi:
            tin += STEP
        last = max(last, t + STEP)
    return tin, ttot, last


def init_state(p, prev_ir, now):
    if prev_ir:
        return dict(prev_ir)
    since = seed_for(p)
    if since is not None and now - since < MAX_BACK:
        return {"since": since, "last": since, "in_s": 0, "tot_s": 0, "fee0": 0.0}
    # posisi baru: mulai hitung sekarang, fee yang sudah ada dijadikan titik nol
    return {"since": now, "last": now, "in_s": 0, "tot_s": 0, "fee0": p.get("fees_usd", 0.0)}


def summary(p, ir):
    h_in, h_tot = ir["in_s"] / 3600, ir["tot_s"] / 3600
    fee = max(0.0, p.get("fees_usd", 0.0) - ir["fee0"])
    val = p.get("value_usd") or 0
    out = {"jam_dihitung": round(h_tot, 2), "jam_inrange": round(h_in, 2),
           "pct_inrange": round(100 * h_in / h_tot, 1) if h_tot else None,
           "fee_dihitung_usd": round(fee, 2)}
    if h_in >= 0.5 and val:
        out["fee_per_jam_inrange_usd"] = round(fee / h_in, 4)
        out["fee_hari_inrange_pct"] = round(100 * fee / h_in * 24 / val, 3)
    if h_tot >= 0.5 and val:
        out["fee_hari_nyata_pct"] = round(100 * fee / h_tot * 24 / val, 3)
    return out


def update(positions, prev_positions, now=None, fetch=candles, deadline=None):
    """Isi field 'ir' (state) dan 'inrange' (ringkasan) ke tiap posisi. Mengembalikan daftar error."""
    now = int(now or time.time())
    prev = {p["key"]: p.get("ir") for p in prev_positions}
    errs, cache = [], {}
    for p in positions:
        p["ir"] = init_state(p, prev.get(p["key"]), now)
    by_coin = {}
    for p in positions:
        c = coin_of(p.get("pair", ""))
        if c and not p.get("stale"):
            by_coin.setdefault(c, []).append(p)
    for c, ps in by_coin.items():
        if deadline and time.time() > deadline:
            errs.append(f"inrange {c}: lewat batas waktu")
            continue
        start = min(p["ir"]["last"] for p in ps)
        try:
            cache[c] = fetch(c, start - STEP, now)
        except Exception as e:  # noqa: BLE001  gagal: state tidak maju, dikejar run berikutnya
            errs.append(f"inrange {c}: {e}")
            continue
        if not cache[c]:
            continue
        hl_now = cache[c][-1][1]
        for p in ps:
            ratio = (p.get("price") or hl_now) / hl_now
            lo, hi = p["range"]
            tin, ttot, last = count(cache[c], p["ir"]["last"], now, lo, hi, ratio)
            p["ir"]["in_s"] += tin
            p["ir"]["tot_s"] += ttot
            p["ir"]["last"] = last
    for p in positions:
        p["inrange"] = summary(p, p["ir"])
    return errs
