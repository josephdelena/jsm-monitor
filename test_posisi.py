"""Tes matematika posisi.py (Uniswap v3)."""
import math
import unittest

import posisi as P


class TestMath(unittest.TestCase):
    def test_signed(self):
        self.assertEqual(P.to_signed(2 ** 256 - 5), -5)
        self.assertEqual(P.to_signed(7), 7)

    def test_below_range_all_token0(self):
        # harga di bawah range -> semua token0
        tl, tu = 100, 200
        sq = int(math.sqrt(1.0001 ** 50) * 2 ** 96)
        a0, a1 = P.amounts(10 ** 18, sq, tl, tu)
        self.assertGreater(a0, 0)
        self.assertEqual(a1, 0)

    def test_above_range_all_token1(self):
        sq = int(math.sqrt(1.0001 ** 300) * 2 ** 96)
        a0, a1 = P.amounts(10 ** 18, sq, 100, 200)
        self.assertEqual(a0, 0)
        self.assertGreater(a1, 0)

    def test_fee_inside_in_range(self):
        # in range: inside = global - outside_lo - outside_up
        self.assertEqual(P.fee_inside(150, 100, 200, 1000, 100, 50), 850)

    def test_fee_inside_below(self):
        # tick < tl: below = global - out_lo
        self.assertEqual(P.fee_inside(50, 100, 200, 1000, 300, 50), (1000 - 700 - 50) % P.M)

    def test_fee_inside_wrap(self):
        v = P.fee_inside(150, 100, 200, 5, 10, 0)
        self.assertEqual(v, (5 - 10) % P.M)

    def test_uncollected(self):
        self.assertEqual(P.uncollected(2 * P.Q128, 30, 10, 7), 7 + 40)
        # wrap
        self.assertEqual(P.uncollected(P.Q128, 5, P.M - 5, 0), 10)

    def test_value_usd_weth_usdc(self):
        # WETH(18)/USDC(6), harga 1774 USDC per WETH
        p_raw = 1774 * 10 ** (6 - 18)
        sq = math.sqrt(p_raw) * 2 ** 96
        val, fee, coin = P.value_usd(0.5 * 10 ** 18, 100 * 10 ** 6, 0, 2 * 10 ** 6, sq, 18, 6, False)
        self.assertAlmostEqual(coin, 1774, places=3)
        self.assertAlmostEqual(val, 0.5 * 1774 + 100, places=3)
        self.assertAlmostEqual(fee, 2, places=6)

    def test_value_usd_stable_token0(self):
        # USDC(6) token0, coin(8) token1, harga coin 64000 USD
        p_raw = (1 / 64000) * 10 ** (8 - 6)
        sq = math.sqrt(p_raw) * 2 ** 96
        val, fee, coin = P.value_usd(1000 * 10 ** 6, 0.01 * 10 ** 8, 0, 0, sq, 6, 8, True)
        self.assertAlmostEqual(coin, 64000, delta=0.01)
        self.assertAlmostEqual(val, 1000 + 640, delta=0.01)


if __name__ == "__main__":
    unittest.main()
