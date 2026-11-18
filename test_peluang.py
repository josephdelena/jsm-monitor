"""Tes matematika peluang.py dengan angka asli 17->18 Nov 2026 (dicek manual di browser)."""
import csv
import os
import tempfile
import unittest

import peluang


class TestFeeRatio(unittest.TestCase):
    def test_orca_cbbtc_matches_browser(self):
        # Orca cbBTC/USDC, 24,03 jam, hasil browser: ±5% = 0,301%/hari
        r = peluang.fee_ratio_day(55045484340483, 34254050614858746,
                                  467017053700380051691, 64, 0.05, 24.03208972222222)
        self.assertAlmostEqual(r * 100, 0.301, places=3)

    def test_uni_avax_matches_browser(self):
        # Uniswap WAVAX/USDC 0,05%, hasil browser: ±5% = 0,708%/hari
        r = peluang.fee_ratio_day(22572343507644244211941979264038980139841,
                                  157612910708271600706074904451,
                                  211454369502841758413951, 128, 0.05, 24.037046666666665)
        self.assertAlmostEqual(r * 100, 0.708, places=3)

    def test_wider_range_less_fee(self):
        a = peluang.fee_ratio_day(10**15, 10**16, 2**64 * 300, 64, 0.05, 24)
        b = peluang.fee_ratio_day(10**15, 10**16, 2**64 * 300, 64, 0.10, 24)
        self.assertGreater(a, b)
        self.assertAlmostEqual(a / b, 2.0, delta=0.1)

    def test_zero_hours(self):
        self.assertEqual(peluang.fee_ratio_day(1, 1, 2**64, 64, 0.05, 0), 0.0)


class TestNet(unittest.TestCase):
    def test_exit_cost_sol_5pct(self):
        # SOL sigma 3,25%: bertahan (5/3,25)^2 = 2,37 hari, rugi 1,45% -> 0,61%/hari
        self.assertAlmostEqual(peluang.exit_cost_day(0.05, 0.0325) * 100, 0.6126, places=3)

    def test_net_with_funding(self):
        n = peluang.net_day(0.0064, 0.001, 0.05, 0.0325)
        self.assertAlmostEqual(n * 100, 0.64 + 0.05 - 0.6126, places=3)

    def test_sigma_zero(self):
        self.assertEqual(peluang.exit_cost_day(0.05, 0), 0.0)


class TestDelta(unittest.TestCase):
    def test_wrap_evm(self):
        self.assertEqual(peluang.growth_delta(5, 2 ** 256 - 5, 128), 10)

    def test_wrap_orca(self):
        self.assertEqual(peluang.growth_delta(5, 2 ** 128 - 5, 64), 10)


class TestPickBase(unittest.TestCase):
    def test_pick(self):
        rows = [{"t": h * 3600} for h in range(0, 200)]
        end = 199 * 3600
        b = peluang.pick_base(rows, end, 24, 18)
        self.assertEqual(b["t"], 175 * 3600)
        b7 = peluang.pick_base(rows, end, 168, 120)
        self.assertEqual(b7["t"], 31 * 3600)

    def test_too_young(self):
        rows = [{"t": h * 3600} for h in range(0, 10)]
        self.assertIsNone(peluang.pick_base(rows, 9 * 3600, 24, 18))


class TestEvaluate(unittest.TestCase):
    def test_end_to_end_csv(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts_utc", "pool", "chain", "fg0", "fg1", "liquidity", "sqrt_price", "tick", "q"])
            w.writerow(["2026-11-17T13:34:21Z", "orca-cbbtc-usdc", "solana", "28069516423831537",
                        "24199782122634841875", "1", "469896855932206737309", "0", "64"])
            w.writerow(["2026-11-18T13:36:16Z", "orca-cbbtc-usdc", "solana",
                        str(28069516423831537 + 55045484340483),
                        str(24199782122634841875 + 34254050614858746), "1",
                        "467017053700380051691", "0", "64"])
        by = peluang.load_csv(path)
        os.remove(path)
        res = peluang.evaluate(by, {"BTC": 0.0233}, {}, {"BTC": 0.0})
        self.assertEqual(len(res), 1)
        r = res[0]
        self.assertEqual(r["window"], "24 jam")
        # ±10% paling bagus untuk BTC, bersih ~+0,01%/hari
        self.assertEqual(r["w"], 0.10)
        self.assertAlmostEqual(r["net"] * 100, 0.01, delta=0.02)
        self.assertIn("orca-cbbtc-usdc", peluang.fmt(res))


if __name__ == "__main__":
    unittest.main()
