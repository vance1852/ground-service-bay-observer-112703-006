"""检查本机设备回放与资料。"""

import json
import threading
import unittest
from urllib.request import Request, urlopen

from bench import load_scene, make_server, read


class BenchTest(unittest.TestCase):
    def test_device_exchange(self):
        rows = load_scene()
        devices = read("devices.json")
        self.assertTrue({row["device"] for row in rows} <= set(devices))
        with make_server(rows, 0) as server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                with urlopen(url + "/events?after=1&limit=2", timeout=4) as reply:
                    self.assertEqual(json.load(reply), {"events": rows[1:3], "cursor": 3})
                with urlopen(url + "/devices", timeout=4) as reply:
                    self.assertEqual(json.load(reply), devices)
                message = json.dumps({"device": rows[0]["device"], "id": "loopback-17", "action": "probe"}).encode()
                with urlopen(Request(url + "/commands", data=message), timeout=4) as reply:
                    self.assertEqual(reply.status, 202)
                    self.assertEqual(json.load(reply)["receipt"], "transport-only")
            finally:
                server.shutdown()
                worker.join()

    def test_material_links(self):
        rows = load_scene()
        by_seq = {row["seq"]: row for row in rows}
        for row in rows:
            for earlier in row.get("refs", []):
                self.assertLess(earlier, row["seq"])
                self.assertIn(earlier, by_seq)
        material_contract(self, rows)


def material_contract(self, rows):
    self.assertEqual(rows[6]["to"], rows[7]["bay"])
    self.assertEqual(rows[6]["from"], rows[8]["bay"])
    self.assertLess(rows[12]["source_epoch"], rows[9]["source_epoch"])
    self.assertEqual(set(rows[13]["claims"]), {"N"})
