#!/usr/bin/env python3
"""JSM monitor (tahap 1): pantau range posisi LP, funding Hyperliquid, scan pool Orca.

Read-only. Tidak pegang wallet. Kirim alarm ke Telegram kalau env
TELEGRAM_BOT_TOKEN dan TELEGRAM_CHAT_ID ada; kalau tidak, cuma print.

Mode:
  python monitor.py check   -> cek range (tiap 5 menit)
  python monitor.py report  -> laporan harian (07:00 WIB)
"""
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "positions.json")
STATE = os.environ.get("JSM_STATE", os.path.join(HERE, "state", "state.json"))
WIB = timezone(timedelta(hours=7))
UA = {"User-Agent": "jsm-monitor/1.0"}


# ---------- http ----------
def http_json(url, body=None, timeout=15, retries=3):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(UA)
    if data is not None:
        headers["Content-Type"] = "application/json"
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"gagal ambil {url}: {last}")


# ---------- harga ----------
def price_orca(pool, invert):
    d = http_json(f"https://api.orca.so/v2/solana/pools/{pool}")["data"]
    p = float(d["price"])
    if not math.isfinite(p) or p <= 0:
        raise RuntimeError("harga orca tidak valid")
    return 1.0 / p if invert else p


def price_binance(symbol, invert):
    # data-api.binance.vision = endpoint data publik Binance (tidak diblok region)
    d = http_json(f"https://data-api.binance.vision/api/v3/ticker/price?symbol={symbol}")
    p = float(d["price"])
    if not math.isfinite(p) or p <= 0:
        raise RuntimeError("harga binance tidak valid")
    return 1.0 / p if invert else p


def get_price(pos):
    if pos["source"] == "orca":
        return price_orca(pos["pool"], pos.get("invert", False))
    if pos["source"] == "binance":
        return price_binance(pos["symbol"], pos.get("invert", False))
    raise ValueError(f"source tidak dikenal: {pos['source']}")


# ---------- logika range (murni, mudah dites) ----------
def classify(price, lower, upper, near_pct, prev_status=None, hyst=0.1):
    """Return (status, jarak_ke_bawah_%, jarak_ke_atas_%).
    status: 'in' | 'near_low' | 'near_high' | 'out_low' | 'out_high'"""
    if lower <= 0 or upper <= lower:
        raise ValueError("range tidak valid")
    d_low = (price / lower - 1.0) * 100.0   # % di atas batas bawah
    d_high = (upper / price - 1.0) * 100.0  # % di bawah batas atas
    if price < lower:
        return "out_low", d_low, d_high
    if price > upper:
        return "out_high", d_low, d_high
    # hysteresis: kalau sebelumnya near, baru dianggap aman setelah lewat near_pct + hyst
    nl = near_pct + (hyst if prev_status == "near_low" else 0.0)
    nh = near_pct + (hyst if prev_status == "near_high" else 0.0)
    if d_low <= nl:
        return "near_low", d_low, d_high
    if d_high <= nh:
        return "near_high", d_low, d_high
    return "in", d_low, d_high


def decide_alert(status, prev, now_ts, out_repeat_min):
    """Kirim alarm kalau status berubah, atau masih di luar range dan sudah lewat out_repeat_min.
    prev = {"status":..., "last_alert": ts} atau None."""
    if prev is None:
        return status != "in"
    if status != prev.get("status"):
        # dari near kembali ke in juga dikabarkan (aman lagi)
        return True
    if status.startswith("out"):
        return now_ts - prev.get("last_alert", 0) >= out_repeat_min * 60
    return False


def fmt_price(p):
    return f"{p:.6f}" if p < 1 else f"{p:,.2f}"


def alert_text(pos, status, price, d_low, d_high):
    rng = f"{fmt_price(pos['lower'])} – {fmt_price(pos['upper'])}"
    head = {
        "in": "✅ AMAN LAGI",
        "near_low": "⚠️ DEKAT BATAS BAWAH",
        "near_high": "⚠️ DEKAT BATAS ATAS",
        "out_low": "🚨 KELUAR RANGE (BAWAH)",
        "out_high": "🚨 KELUAR RANGE (ATAS)",
    }[status]
    lines = [
        f"{head} — {pos['label']}",
        f"Harga: {fmt_price(price)} {pos['unit']}",
        f"Range: {rng}",
        f"Jarak ke bawah: {d_low:+.2f}% | ke atas: {d_high:+.2f}%",
    ]
    if status == "in":
        lines.append("")
        lines.append("Kalau sebelumnya sempat KELUAR dan SL/Stop Market GMX sudah kena: "
                     "ukuran short sekarang salah. Kembalikan short ke ukuran awal "
                     f"({pos.get('hedge_usd', 'lihat catatan')}) dan pasang ulang order, atau tutup set.")
    if status.startswith("out"):
        lines.append("")
        lines.append("TUTUP SEKARANG (bareng):")
        for i, a in enumerate(pos.get("close_actions", []), 1):
            lines.append(f"{i}. {a}")
    return "\n".join(lines)


# ---------- alarm harga (posisi lending) ----------
def classify_price_alert(price, below, prev_status=None, hyst_pct=0.5):
    """Return 'below' kalau harga <= batas, 'ok' kalau di atas.
    Hysteresis: dari 'below' baru dianggap 'ok' lagi setelah naik > batas + hyst_pct%."""
    if below <= 0:
        raise ValueError("batas harga tidak valid")
    limit = below * (1 + hyst_pct / 100.0) if prev_status == "below" else below
    return "below" if price <= limit else "ok"


def price_alert_text(al, status, price):
    if status == "ok":
        head = "✅ AMAN LAGI"
    else:
        head = "🚨 HARGA TURUN KE BATAS WASPADA"
    lines = [f"{head} — {al['label']}",
             f"Harga {al['symbol']}: {fmt_price(price)} (batas waspada {fmt_price(al['below'])})"]
    if al.get("note"):
        lines.append(al["note"])
    return "\n".join(lines)


# ---------- telegram ----------
def send(text):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    print("----\n" + text)
    if not tok or not chat:
        print("(TELEGRAM_BOT_TOKEN/CHAT_ID belum diisi — hanya print)")
        return False
    url = f"https://api.telegram.org/bot{tok.strip()}/sendMessage"
    body = json.dumps({"chat_id": chat.strip(), "text": text,
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(url, data=body, headers={**UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        return True
    except urllib.error.HTTPError as e:
        # tampilkan alasan dari Telegram, tanpa token
        try:
            desc = json.loads(e.read().decode()).get("description", "")
        except Exception:  # noqa: BLE001
            desc = ""
        print(f"gagal kirim telegram: HTTP {e.code} {desc}")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"gagal kirim telegram: {type(e).__name__}")
        return False


# ---------- state ----------
def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, STATE)


# ---------- funding hyperliquid ----------
def hl_funding(coins=("SOL", "BTC", "ETH")):
    meta, ctxs = http_json("https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs"})
    out = {}
    for u, c in zip(meta["universe"], ctxs):
        if u["name"] in coins:
            h = float(c["funding"])  # per jam; positif = long bayar short
            out[u["name"]] = {"hour": h, "day_pct": h * 24 * 100, "year_pct": h * 24 * 365 * 100}
    return out


# ---------- scan pool orca ----------
def orca_scan(cfg):
    sc = cfg["scan"]
    majors = set(sc["majors"])
    d = http_json("https://api.orca.so/v2/solana/pools?sortBy=volume&sortDirection=desc&size=200")
    rows = []
    for p in d.get("data", []):
        try:
            a, b = p["tokenA"]["symbol"], p["tokenB"]["symbol"]
            tvl = float(p["tvlUsdc"])
            if a not in majors or b not in majors or tvl < sc["min_tvl_usd"]:
                continue
            if {a, b} <= {"USDC", "USDT"}:
                continue
            y1 = float(p["stats"]["24h"]["yieldOverTvl"]) * 100
            y7 = float(p["stats"]["7d"]["yieldOverTvl"]) * 100 / 7
            rows.append({"pair": f"{a}/{b}", "fee": p["feeRate"] / 1e4, "tvl": tvl,
                         "y1": y1, "y7": y7, "addr": p["address"]})
        except Exception:  # noqa: BLE001
            continue
    rows.sort(key=lambda r: r["y1"], reverse=True)
    return rows


# ---------- mode ----------
def run_check(cfg):
    st = load_state()
    now = time.time()
    errors = []
    for pos in cfg["positions"]:
        if pos.get("disabled"):
            continue
        try:
            price = get_price(pos)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{pos['label']}: {e}")
            continue
        prev = st.get(pos["id"])
        status, d_low, d_high = classify(price, pos["lower"], pos["upper"], cfg["near_pct"],
                                         (prev or {}).get("status"))
        print(f"{pos['id']}: {fmt_price(price)} status={status} low={d_low:+.2f}% high={d_high:+.2f}%")
        entry = {"status": status, "price": price, "ts": now,
                 "last_alert": (prev or {}).get("last_alert", 0)}
        if decide_alert(status, prev, now, cfg["out_repeat_min"]):
            if send(alert_text(pos, status, price, d_low, d_high)):
                entry["last_alert"] = now
            elif not os.environ.get("TELEGRAM_BOT_TOKEN"):
                entry["last_alert"] = now  # mode print: tetap dedupe
            else:
                # kirim gagal: jangan majukan status supaya run berikutnya coba lagi
                entry["status"] = (prev or {}).get("status")
        st[pos["id"]] = entry
    for al in cfg.get("price_alerts", []):
        if al.get("disabled"):
            continue
        key = "alert:" + str(al.get("id"))
        prev = st.get(key)
        try:
            price = price_binance(al["symbol"], False)
            status = classify_price_alert(price, float(al["below"]), (prev or {}).get("status"))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{al.get('label', key)}: {e}")
            continue
        print(f"{key}: {fmt_price(price)} status={status}")
        entry = {"status": status, "price": price, "ts": now,
                 "last_alert": (prev or {}).get("last_alert", 0)}
        if prev is None or prev.get("status") is None:
            fire = status == "below"
        elif status != prev.get("status"):
            fire = True
        else:
            fire = status == "below" and now - prev.get("last_alert", 0) >= cfg["out_repeat_min"] * 60
        if fire:
            if send(price_alert_text(al, status, price)):
                entry["last_alert"] = now
            elif not os.environ.get("TELEGRAM_BOT_TOKEN"):
                entry["last_alert"] = now
            else:
                entry["status"] = (prev or {}).get("status")
        st[key] = entry
    # error berturut-turut -> kabari sekali per jam
    if errors:
        if now - st.get("_last_err_alert", 0) > 3600:
            ok = send("❗ Pemantau JSM gagal ambil harga:\n" + "\n".join(errors))
            if ok or not os.environ.get("TELEGRAM_BOT_TOKEN"):
                st["_last_err_alert"] = now
    save_state(st)


def run_report(cfg):
    lines = [f"📊 Laporan harian JSM — {datetime.now(WIB):%d %b %Y %H:%M} WIB", ""]
    for pos in cfg["positions"]:
        if pos.get("disabled"):
            continue
        try:
            price = get_price(pos)
            status, d_low, d_high = classify(price, pos["lower"], pos["upper"], cfg["near_pct"])
            tag = {"in": "in range", "near_low": "dekat bawah", "near_high": "dekat atas",
                   "out_low": "KELUAR (bawah)", "out_high": "KELUAR (atas)"}[status]
            lines.append(f"• {pos['label']}: {fmt_price(price)} — {tag} "
                         f"(bawah {d_low:+.2f}%, atas {d_high:+.2f}%)")
        except Exception as e:  # noqa: BLE001
            lines.append(f"• {pos['label']}: gagal ambil harga ({e})")
    try:
        f = hl_funding()
        lines.append("")
        lines.append("Funding Hyperliquid (short dibayar kalau +):")
        for k in ("SOL", "BTC", "ETH"):
            if k in f:
                lines.append(f"• {k}: {f[k]['day_pct']:+.3f}%/hari ({f[k]['year_pct']:+.1f}%/thn)")
    except Exception as e:  # noqa: BLE001
        lines.append(f"Funding: gagal ({e})")
    try:
        rows = orca_scan(cfg)
        lines.append("")
        lines.append("Pool Orca koin besar (fee 24j / rata 7h, % TVL per hari):")
        for r in rows[:6]:
            lines.append(f"• {r['pair']} {r['fee']:.2f}%: {r['y1']:.3f}% / {r['y7']:.3f}% "
                         f"(TVL ${r['tvl']/1e6:.1f}jt)")
        hot = [r for r in rows if r["y1"] >= cfg["scan"]["alert_yield_pct_day"]]
        if hot:
            lines.append("")
            lines.append("🔥 Lagi ramai (≥ %.2f%%/hari level pool): " % cfg["scan"]["alert_yield_pct_day"]
                         + ", ".join(r["pair"] for r in hot))
    except Exception as e:  # noqa: BLE001
        lines.append(f"Scan Orca: gagal ({e})")
    try:
        import scanner
        res, n = scanner.run(top=6)
        lines.append("")
        lines.append(scanner.fmt(res, n))
    except Exception as e:  # noqa: BLE001
        lines.append(f"Scanner: gagal ({e})")
    send("\n".join(lines))
    # peluang dikirim terpisah supaya pesan tidak lewat batas 4096 karakter Telegram
    try:
        import peluang
        pres, perrs = peluang.run()
        send(peluang.fmt(pres, perrs))
        hot = peluang.alerts(pres)
        if hot:
            send(peluang.fmt_alert(hot))
    except Exception as e:  # noqa: BLE001
        send(f"💡 Peluang: gagal ({e})")


def main():
    with open(CONFIG) as f:
        cfg = json.load(f)
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "check":
        run_check(cfg)
    elif mode == "report":
        run_report(cfg)
    else:
        sys.exit("mode: check | report")


if __name__ == "__main__":
    main()
