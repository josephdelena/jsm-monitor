#!/usr/bin/env python3
"""Baca posisi Orca Whirlpool milik wallet Solana langsung dari blockchain (read-only).

Langkah: NFT di wallet (getTokenAccountsByOwner) -> alamat Position (PDA) -> data posisi
(likuiditas, tick, checkpoint fee) -> data pool (Orca API) -> tick array (fee di luar range)
-> nilai posisi (USD) + fee belum ditarik (USD).
"""
import base64
import hashlib
import json
import struct
import time
import urllib.request

OWNERS_SOL = ["BWwccZawD8upih6ubKUQrauD3KvgmsWsypc5XFGz8mv6",  # alamat publik wallet Solana Ef
              "DMnuhZZTVuh1Cqiw6oukSu98bA96Sx9pg3Qq5LRNGfAU"]  # wallet Solana kedua (19 Nov)
OWNER_SOL = OWNERS_SOL[0]
WHIRLPOOL = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
TOKEN_PROGRAMS = ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]
RPC = ["https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com"]
STABLE = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
TICK_ARRAY_SIZE = 88
TICK_BYTES = 113
FIXED_TA_LEN = 8 + 4 + TICK_ARRAY_SIZE * TICK_BYTES + 32  # 9988
U128 = 2 ** 128
UA = {"User-Agent": "jsm-monitor/1.0", "Content-Type": "application/json"}
_DL = [float("inf")]  # batas waktu run ini

# ---------- base58 ----------
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s):
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def b58encode(b):
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * pad + out


# ---------- ed25519: cek titik di kurva (untuk PDA) ----------
_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def on_curve(b):
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    u = (y * y - 1) % _P
    v = (_D * y * y + 1) % _P
    x = (u * pow(v, 3, _P) * pow(u * pow(v, 7, _P), (_P - 5) // 8, _P)) % _P
    vx2 = (v * x * x) % _P
    if vx2 == u % _P:
        return True
    if vx2 == (-u) % _P:
        return True
    return False


def find_pda(seeds, program):
    prog = b58decode(program)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + prog + b"ProgramDerivedAddress").digest()
        if not on_curve(h):
            return b58encode(h)
    raise RuntimeError("PDA tidak ketemu")


# ---------- matematika (dites) ----------
def amounts(L, sqrt_q64, tl, tu):
    s = sqrt_q64 / 2 ** 64
    sa, sb = 1.0001 ** (tl / 2), 1.0001 ** (tu / 2)
    sc = min(max(s, sa), sb)
    return L * (1 / sc - 1 / sb), L * (sc - sa)


def fee_inside(tick, tl, tu, glob, out_lo, out_up):
    below = out_lo if tick >= tl else (glob - out_lo) % U128
    above = out_up if tick < tu else (glob - out_up) % U128
    return (glob - below - above) % U128


def owed(L, inside, checkpoint, prev_owed):
    return prev_owed + (L * ((inside - checkpoint) % U128) >> 64)


def tick_array_start(tick, spacing):
    span = spacing * TICK_ARRAY_SIZE
    return (tick // span) * span


def parse_position(data):
    o = 8
    whirlpool = b58encode(data[o:o + 32]); o += 32
    mint = b58encode(data[o:o + 32]); o += 32
    L = int.from_bytes(data[o:o + 16], "little"); o += 16
    tl, tu = struct.unpack_from("<ii", data, o); o += 8
    cp_a = int.from_bytes(data[o:o + 16], "little"); o += 16
    owed_a = int.from_bytes(data[o:o + 8], "little"); o += 8
    cp_b = int.from_bytes(data[o:o + 16], "little"); o += 16
    owed_b = int.from_bytes(data[o:o + 8], "little")
    return {"whirlpool": whirlpool, "mint": mint, "L": L, "tl": tl, "tu": tu,
            "cp_a": cp_a, "owed_a": owed_a, "cp_b": cp_b, "owed_b": owed_b}


def parse_whirlpool(data):
    """Bagian akun Whirlpool yang dipakai (offset dicek ke data on-chain 19 Nov 2026)."""
    u = lambda o, n: int.from_bytes(data[o:o + n], "little")  # noqa: E731
    return {"tickSpacing": u(41, 2), "feeRate": u(45, 2), "sqrtPrice": u(65, 16),
            "tickCurrentIndex": struct.unpack_from("<i", data, 81)[0],
            "feeGrowthGlobalA": u(165, 16), "feeGrowthGlobalB": u(245, 16)}


def parse_tick(data, start, tick, spacing):
    """fee_growth_outside_a/b dari tick array bentuk lama (ukuran tetap). None kalau bentuk lain."""
    if len(data) != FIXED_TA_LEN:
        return None
    idx = (tick - start) // spacing
    o = 8 + 4 + idx * TICK_BYTES
    o += 1 + 16 + 16  # initialized, liquidity_net, liquidity_gross
    fa = int.from_bytes(data[o:o + 16], "little")
    fb = int.from_bytes(data[o + 16:o + 32], "little")
    return fa, fb


# ---------- jaringan ----------
def rpc(method, params, timeout=8):
    if time.time() > _DL[0]:
        raise RuntimeError("lewat batas waktu")
    last = None
    for url in RPC:
        try:
            body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res = json.loads(r.read().decode())
            if "error" in res:
                raise RuntimeError(str(res["error"])[:150])
            return res["result"]
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"solana rpc {method}: {last}")


def get_json(url, timeout=8):
    if time.time() > _DL[0]:
        raise RuntimeError("lewat batas waktu")
    req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def accounts(addrs):
    out = {}
    for i in range(0, len(addrs), 100):
        chunk = addrs[i:i + 100]
        res = rpc("getMultipleAccounts", [chunk, {"encoding": "base64"}])
        for a, v in zip(chunk, res["value"]):
            if v:
                out[a] = {"owner": v["owner"], "data": base64.b64decode(v["data"][0])}
    return out


def read_orca(deadline):
    _DL[0] = deadline
    mints = []
    for ow in OWNERS_SOL:
        for prog in TOKEN_PROGRAMS:
            res = rpc("getTokenAccountsByOwner", [ow, {"programId": prog}, {"encoding": "jsonParsed"}])
            for acc in res["value"]:
                info = acc["account"]["data"]["parsed"]["info"]
                ta = info["tokenAmount"]
                if ta["decimals"] == 0 and ta["amount"] == "1":
                    mints.append(info["mint"])
    pdas = {find_pda([b"position", b58decode(m)], WHIRLPOOL): m for m in mints}
    accs = accounts(list(pdas))
    out, notes = [], []
    pool_cache = {}
    for addr, acc in accs.items():
        if time.time() > deadline:
            raise RuntimeError("lewat batas waktu")
        if acc["owner"] != WHIRLPOOL or len(acc["data"]) < 160:
            continue
        p = parse_position(acc["data"])
        if p["L"] == 0:
            continue
        if p["whirlpool"] not in pool_cache:
            # data statis (token, desimal) dari Orca API; harga & fee growth dibaca on-chain
            pool_cache[p["whirlpool"]] = get_json(f"https://api.orca.so/v2/solana/pools/{p['whirlpool']}")["data"]
        w = pool_cache[p["whirlpool"]]
        wacc = accounts([p["whirlpool"]]).get(p["whirlpool"])
        if not wacc:
            raise RuntimeError("akun whirlpool tidak terbaca")
        live = parse_whirlpool(wacc["data"])
        sq, tick, sp = live["sqrtPrice"], live["tickCurrentIndex"], live["tickSpacing"]
        ga, gb = live["feeGrowthGlobalA"], live["feeGrowthGlobalB"]
        da, db = int(w["tokenA"]["decimals"]), int(w["tokenB"]["decimals"])
        mint_a, mint_b = w["tokenA"]["address"], w["tokenB"]["address"]
        a_stable = mint_a in STABLE
        if not a_stable and mint_b not in STABLE:
            notes.append(f"orca:{addr}: pair tanpa USDC, dilewati")
            continue
        # fee belum ditarik (butuh tick array); kalau bentuk tick array baru, pakai fee yang sudah tercatat saja
        fee_note = None
        starts = {tick_array_start(p["tl"], sp), tick_array_start(p["tu"], sp)}
        ta_addr = {s: find_pda([b"tick_array", b58decode(p["whirlpool"]), str(s).encode()], WHIRLPOOL) for s in starts}
        tas = accounts(list(ta_addr.values()))
        lo = tas.get(ta_addr[tick_array_start(p["tl"], sp)])
        up = tas.get(ta_addr[tick_array_start(p["tu"], sp)])
        tlo = parse_tick(lo["data"], tick_array_start(p["tl"], sp), p["tl"], sp) if lo else None
        tup = parse_tick(up["data"], tick_array_start(p["tu"], sp), p["tu"], sp) if up else None
        ins_a = fee_inside(tick, p["tl"], p["tu"], ga, tlo[0], tup[0]) if tlo and tup else None
        ins_b = fee_inside(tick, p["tl"], p["tu"], gb, tlo[1], tup[1]) if tlo and tup else None
        sane = ins_a is not None and (ins_a - p["cp_a"]) % U128 < 2 ** 127 and (ins_b - p["cp_b"]) % U128 < 2 ** 127
        if sane:
            fa = owed(p["L"], ins_a, p["cp_a"], p["owed_a"])
            fb = owed(p["L"], ins_b, p["cp_b"], p["owed_b"])
        else:
            fa, fb = p["owed_a"], p["owed_b"]
            fee_note = "fee belum ditarik tidak terbaca (bentuk tick array baru), hanya fee tercatat"
        a, b = amounts(p["L"], sq, p["tl"], p["tu"])
        price_b_per_a = (sq / 2 ** 64) ** 2 * 10 ** (da - db)
        ha, hb, fha, fhb = a / 10 ** da, b / 10 ** db, fa / 10 ** da, fb / 10 ** db
        if a_stable:
            coin = 1 / price_b_per_a
            val, fusd = ha + hb * coin, fha + fhb * coin
            lo_px = 1 / (1.0001 ** p["tu"] * 10 ** (da - db))
            up_px = 1 / (1.0001 ** p["tl"] * 10 ** (da - db))
            sym = w["tokenB"]["symbol"]
        else:
            coin = price_b_per_a
            val, fusd = ha * coin + hb, fha * coin + fhb
            lo_px = 1.0001 ** p["tl"] * 10 ** (da - db)
            up_px = 1.0001 ** p["tu"] * 10 ** (da - db)
            sym = w["tokenA"]["symbol"]
        item = {"key": f"orca:{addr}", "chain": "solana", "dex": "Orca", "id": addr,
                "pair": f"{sym}/USDC", "fee_tier": live["feeRate"] / 1e6, "pool": p["whirlpool"],
                "price": round(coin, 6), "range": [round(lo_px, 6), round(up_px, 6)],
                "in_range": p["tl"] <= tick < p["tu"], "value_usd": round(val, 2), "fees_usd": round(fusd, 2),
                "coin_amount": round(hb if a_stable else ha, 6), "usdc_amount": round(ha if a_stable else hb, 2)}
        if fee_note:
            item["note"] = fee_note
        out.append(item)
    return out, notes
