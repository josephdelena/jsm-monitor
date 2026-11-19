"""Tes orca_pos.py: PDA, base58, matematika fee, parsing."""
import struct
import unittest

import orca_pos as o


class TestOrca(unittest.TestCase):
    def test_pda_whirlpool_cbbtc_usdc(self):
        w = o.find_pda([b"whirlpool", o.b58decode("2LecshUwdy9xi7meFgHtFJQNSKk4KdTrcpvaB56dP2NQ"),
                        o.b58decode("cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij"),
                        o.b58decode("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"), struct.pack("<H", 4)], o.WHIRLPOOL)
        self.assertEqual(w, "HxA6SKW5qA4o12fjVgTpXdq2YnZ5Zv1s7SB4FFomsyLM")

    def test_b58_roundtrip(self):
        a = "BWwccZawD8upih6ubKUQrauD3KvgmsWsypc5XFGz8mv6"
        self.assertEqual(o.b58encode(o.b58decode(a)), a)

    def test_owed_q64(self):
        self.assertEqual(o.owed(2 ** 64, 30, 10, 5), 5 + 20)
        self.assertEqual(o.owed(2 ** 64, 5, o.U128 - 5, 0), 10)

    def test_tick_array_start(self):
        self.assertEqual(o.tick_array_start(64632, 4), 64416)
        self.assertEqual(o.tick_array_start(-5, 4), -352)

    def test_parse_position_roundtrip(self):
        data = bytearray(216)
        o_ = 8
        data[o_:o_ + 32] = o.b58decode("HxA6SKW5qA4o12fjVgTpXdq2YnZ5Zv1s7SB4FFomsyLM"); o_ += 32
        data[o_:o_ + 32] = o.b58decode("BWwccZawD8upih6ubKUQrauD3KvgmsWsypc5XFGz8mv6"); o_ += 32
        data[o_:o_ + 16] = (12345).to_bytes(16, "little"); o_ += 16
        struct.pack_into("<ii", data, o_, -100, 200); o_ += 8
        data[o_:o_ + 16] = (7).to_bytes(16, "little"); o_ += 16
        data[o_:o_ + 8] = (3).to_bytes(8, "little")
        p = o.parse_position(bytes(data))
        self.assertEqual((p["L"], p["tl"], p["tu"], p["cp_a"], p["owed_a"]), (12345, -100, 200, 7, 3))
        self.assertEqual(p["whirlpool"], "HxA6SKW5qA4o12fjVgTpXdq2YnZ5Zv1s7SB4FFomsyLM")

    def test_parse_tick_fixed(self):
        data = bytearray(o.FIXED_TA_LEN)
        start, sp, tick = 64416, 4, 64432
        off = 8 + 4 + ((tick - start) // sp) * o.TICK_BYTES + 33
        data[off:off + 16] = (111).to_bytes(16, "little")
        data[off + 16:off + 32] = (222).to_bytes(16, "little")
        self.assertEqual(o.parse_tick(bytes(data), start, tick, sp), (111, 222))
        self.assertIsNone(o.parse_tick(b"x" * 100, start, tick, sp))



class TestWhirlpool(unittest.TestCase):
    def test_parse_live_account(self):
        import base64
        d = base64.b64decode("P5XRDOGAYwkT5EH4ORPKaLBjT7Al/eqohzfoQRDRJV41ezN33e4czf0EAAQAkAEUBc2TEOkGAQAAAAAAAAAAAAC14DpHLB/8DxkAAAAAAAAArvsAANgSAAAAAAAAh5wvAAAAAAAJHnPRelUm1Ejlia6lr+fCLNYcW2aoakJ6smIwlRTlXNegNMjalukgHMBOzdjr38gjMR6GCsU5bXZXM56yDG6ryFnB2KQUZAAAAAAAAAAAAMb6evO+2606PWXzaqvJdDGxu+TC0vbg5HymAgNFL11hRBDzwYdmRJRUSr6cT+zKSY4yt6cO+kNSgc7Cpbcox4SzYAZkyYOwUAEAAAAAAAAAvF7+ag==")
        w = o.parse_whirlpool(d)
        self.assertEqual(w["tickSpacing"], 4)
        self.assertEqual(w["feeRate"], 400)
        self.assertEqual(w["tickCurrentIndex"], 64430)
        self.assertEqual(w["feeGrowthGlobalA"], 28170195914807752)  # sama dengan Orca API


if __name__ == "__main__":
    unittest.main()
