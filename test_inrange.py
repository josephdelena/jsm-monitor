import unittest

import inrange as ir


def P(key="k", pair="cbBTC/USDC", chain="solana", rng=(100, 110), price=100.0, fees=0.0, val=1000.0, **kw):
    return {"key": key, "pair": pair, "chain": chain, "range": list(rng), "price": price,
            "fees_usd": fees, "value_usd": val, **kw}


def fake(prices, t0):
    return lambda coin, s, e: [(t0 + i * 300, c) for i, c in enumerate(prices)]


class T(unittest.TestCase):
    def test_wib(self):
        self.assertEqual(ir.wib_ts("2026-11-19 07:00"), ir.wib_ts("2026-11-19 00:00") + 7 * 3600)
        self.assertEqual(ir.wib_ts("1970-01-01 07:00"), 0)

    def test_count_basic(self):
        cs = [(0, 99), (300, 105), (600, 109.9), (900, 110), (1200, 105)]
        self.assertEqual(ir.count(cs, 0, 1500, 100, 110, 1.0), (900, 1500, 1500))

    def test_count_skips_partial_and_before(self):
        cs = [(0, 105), (300, 105), (600, 105)]
        # mulai 300, sekarang 800: candle 600 belum selesai
        self.assertEqual(ir.count(cs, 300, 800, 100, 110, 1.0), (300, 300, 600))

    def test_new_position_fee_zero_point(self):
        p = P(fees=5.0)
        now = 10_000
        errs = ir.update([p], [], now=now, fetch=fake([105] * 3, now))
        self.assertEqual(errs, [])
        self.assertEqual(p["ir"]["fee0"], 5.0)
        self.assertEqual(p["ir"]["since"], now)

    def test_accumulates_across_runs(self):
        t0 = 1_000_000
        p1 = P(fees=0.0)
        p1["ir"] = {"since": t0, "last": t0, "in_s": 0, "tot_s": 0, "fee0": 0.0}
        prices = [105] * 12 + [99] * 12  # 1 jam in, 1 jam out
        p2 = P(fees=2.4, price=99.0)  # harga pool = harga HL terakhir
        ir.update([p2], [p1], now=t0 + 7200, fetch=fake(prices, t0))
        s = p2["inrange"]
        self.assertEqual(s["jam_inrange"], 1.0)
        self.assertEqual(s["jam_dihitung"], 2.0)
        self.assertEqual(s["pct_inrange"], 50.0)
        self.assertAlmostEqual(s["fee_per_jam_inrange_usd"], 2.4)
        self.assertAlmostEqual(s["fee_hari_inrange_pct"], 5.76)  # 2.4*24/1000
        self.assertAlmostEqual(s["fee_hari_nyata_pct"], 2.88)

    def test_ratio_adjusts_to_pool_price(self):
        t0 = 0
        p = P(price=101.0)  # pool 1% di atas Hyperliquid
        p["ir"] = {"since": t0, "last": t0, "in_s": 0, "tot_s": 0, "fee0": 0.0}
        new = P(price=101.0)
        # HL 99.5 -> pool 100.5 (in range); harga terakhir HL 100 -> ratio 1.01
        ir.update([new], [p], now=600, fetch=fake([99.5, 100], 0))
        self.assertEqual(new["ir"]["in_s"], 600)

    def test_fetch_error_keeps_state(self):
        old = P()
        old["ir"] = {"since": 0, "last": 0, "in_s": 300, "tot_s": 300, "fee0": 0.0}
        new = P()

        def boom(*a):
            raise RuntimeError("down")
        errs = ir.update([new], [old], now=3600, fetch=boom)
        self.assertTrue(errs)
        self.assertEqual(new["ir"]["last"], 0)
        self.assertEqual(new["ir"]["in_s"], 300)

    def test_stale_not_counted(self):
        old = P()
        old["ir"] = {"since": 0, "last": 0, "in_s": 0, "tot_s": 0, "fee0": 0.0}
        new = P(stale=True)
        ir.update([new], [old], now=3600, fetch=fake([105] * 12, 0))
        self.assertEqual(new["ir"]["tot_s"], 0)

    def test_seed_by_pair(self):
        p = P(key="orca:XYZ", pair="SOL/USDC")
        st = ir.init_state(p, None, ir.wib_ts("2026-11-19 20:00"))
        self.assertEqual(st["since"], ir.wib_ts("2026-11-19 19:09"))
        self.assertEqual(st["fee0"], 0.0)

    def test_unknown_coin_skipped(self):
        p = P(pair="?/USDC")
        errs = ir.update([p], [], now=100, fetch=fake([], 0))
        self.assertEqual(errs, [])
        self.assertIn("inrange", p)


if __name__ == "__main__":
    unittest.main()
