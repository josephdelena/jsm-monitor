import json, os, tempfile, unittest
from unittest import mock
os.environ["JSM_STATE"] = os.path.join(tempfile.mkdtemp(), "s", "state.json")
os.environ.pop("TELEGRAM_BOT_TOKEN", None)
import monitor as m

FIXTURE = {"near_pct":0.4,"out_repeat_min":30,"positions":[
 {"id":"orca-sol-cbbtc","label":"Orca SOL/cbBTC","source":"orca","pool":"x","invert":True,"unit":"SOL per cbBTC","lower":885.86,"upper":927.93,
  "close_actions":["Tarik pool","tutup short SOL $53.89","tutup short BTC"]},
 {"id":"aero","label":"Aero","source":"binance","symbol":"X","unit":"x","lower":0.02738,"upper":0.02871,"close_actions":["Tarik pool"]}]}
def load_cfg(): return json.loads(json.dumps(FIXTURE))

class T(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(m.classify(900, 885.86, 927.93, 0.4)[0], "in")
        self.assertEqual(m.classify(887, 885.86, 927.93, 0.4)[0], "near_low")
        self.assertEqual(m.classify(925, 885.86, 927.93, 0.4)[0], "near_high")
        self.assertEqual(m.classify(885, 885.86, 927.93, 0.4)[0], "out_low")
        self.assertEqual(m.classify(930, 885.86, 927.93, 0.4)[0], "out_high")
        self.assertEqual(m.classify(0.0280, 0.02738, 0.02871, 0.4)[0], "in")
        self.assertEqual(m.classify(0.02870, 0.02738, 0.02871, 0.4)[0], "near_high")
        with self.assertRaises(ValueError): m.classify(1, 2, 1, 0.4)
    def test_decide(self):
        self.assertFalse(m.decide_alert("in", None, 1000, 30))
        self.assertTrue(m.decide_alert("near_low", None, 1000, 30))
        self.assertFalse(m.decide_alert("in", {"status":"in","last_alert":0}, 1000, 30))
        self.assertTrue(m.decide_alert("in", {"status":"near_low","last_alert":0}, 1000, 30))
        self.assertFalse(m.decide_alert("out_low", {"status":"out_low","last_alert":1000}, 1000+29*60, 30))
        self.assertTrue(m.decide_alert("out_low", {"status":"out_low","last_alert":1000}, 1000+31*60, 30))
    def test_check_flow(self):
        cfg = load_cfg()
        prices = iter([900.0, 0.0280, 884.0, 0.0280, 884.0, 0.0280])
        sent = []
        with mock.patch.object(m, "get_price", side_effect=lambda p: next(prices)), \
             mock.patch.object(m, "send", side_effect=lambda t: sent.append(t) or False):
            m.run_check(cfg); self.assertEqual(len(sent), 0)
            m.run_check(cfg); self.assertEqual(len(sent), 1); self.assertIn("KELUAR RANGE (BAWAH)", sent[0]); self.assertIn("tutup short SOL", sent[0])
            m.run_check(cfg); self.assertEqual(len(sent), 1)  # dedupe <30 menit
    def test_orca_invert(self):
        with mock.patch.object(m, "http_json", return_value={"data":{"price":"0.0011047"}}):
            self.assertAlmostEqual(m.price_orca("x", True), 905.22, places=1)
    def test_hl(self):
        fake = [{"universe":[{"name":"BTC"},{"name":"ETH"},{"name":"SOL"}]},[{"funding":"0.0000125"},{"funding":"-0.00001"},{"funding":"0.00001"}]]
        with mock.patch.object(m, "http_json", return_value=fake):
            f = m.hl_funding(); self.assertAlmostEqual(f["BTC"]["day_pct"], 0.03)
    def test_err_alert(self):
        cfg = load_cfg(); sent=[]
        with mock.patch.object(m, "get_price", side_effect=RuntimeError("x")), \
             mock.patch.object(m, "send", side_effect=lambda t: sent.append(t) or False):
            m.run_check(cfg); m.run_check(cfg)
        self.assertEqual(len(sent), 1)
class T2(unittest.TestCase):
    def test_hyst(self):
        # 0.45% dari bawah: tanpa prev = in, dengan prev near_low tetap near (hysteresis)
        p = 885.86*1.0045
        self.assertEqual(m.classify(p, 885.86, 927.93, 0.4)[0], "in")
        self.assertEqual(m.classify(p, 885.86, 927.93, 0.4, "near_low")[0], "near_low")
    def test_failed_send_retries(self):
        cfg = load_cfg(); sent=[]
        os.environ["TELEGRAM_BOT_TOKEN"]="x"; os.environ["TELEGRAM_CHAT_ID"]="y"
        try:
            prices = iter([884.0, 0.028, 884.0, 0.028])
            res = iter([False, True])
            with mock.patch.object(m, "get_price", side_effect=lambda p: next(prices)), \
                 mock.patch.object(m, "send", side_effect=lambda t: sent.append(t) or next(res)):
                m.save_state({}); m.run_check(cfg); m.run_check(cfg)
            self.assertEqual(len(sent), 2)  # gagal lalu dicoba lagi
        finally:
            os.environ.pop("TELEGRAM_BOT_TOKEN"); os.environ.pop("TELEGRAM_CHAT_ID")
    def test_nan(self):
        with mock.patch.object(m, "http_json", return_value={"data":{"price":"nan"}}):
            with self.assertRaises(RuntimeError): m.price_orca("x", True)
class TPriceAlert(unittest.TestCase):
    def test_classify_and_hysteresis(self):
        self.assertEqual(m.classify_price_alert(0.62, 0.63), "below")
        self.assertEqual(m.classify_price_alert(0.63, 0.63), "below")
        self.assertEqual(m.classify_price_alert(0.64, 0.63), "ok")
        self.assertEqual(m.classify_price_alert(0.632, 0.63, "below"), "below")
        self.assertEqual(m.classify_price_alert(0.64, 0.63, "below"), "ok")

    def test_text(self):
        t = m.price_alert_text({"label": "L", "symbol": "SUIUSDT", "below": 0.63, "note": "N"}, "below", 0.62)
        self.assertIn("WASPADA", t); self.assertIn("N", t)

    def test_run_check_fires_once_then_repeat_and_recover(self):
        cfg = {"near_pct": 0.4, "out_repeat_min": 30, "positions": [],
               "price_alerts": [{"id": "sui", "label": "NAVI", "symbol": "SUIUSDT", "below": 0.63}]}
        sent = []
        prices = iter([0.62, 0.62, 0.62, 0.70])
        times = iter([1000, 1100, 1000 + 31 * 60, 1000 + 32 * 60])
        if os.path.exists(m.STATE): os.remove(m.STATE)
        with mock.patch.object(m, "price_binance", side_effect=lambda s, i: next(prices)), \
             mock.patch.object(m, "send", side_effect=lambda t: sent.append(t) or True), \
             mock.patch.object(m.time, "time", side_effect=lambda: next(times)):
            for _ in range(4):
                m.run_check(cfg)
        self.assertEqual(len(sent), 3)
        self.assertIn("WASPADA", sent[0]); self.assertIn("WASPADA", sent[1]); self.assertIn("AMAN LAGI", sent[2])



unittest.main(verbosity=1)
