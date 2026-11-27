"""相邻检修位占用与准入的判定检查。

场景全部使用合成门侧、压力与近场读头；不连接真实控制设备。
"""

import json
import threading
import unittest
from urllib.request import Request, urlopen

from yard.engine import YardEngine
from yard.scene import Observation, load_devices, load_policy, load_scene
from yard.service import IngestService, make_server

DEVICES = load_devices()
POLICY = load_policy()


def obs(seq, device, kind, at, arrived=None, **data):
    if arrived is not None:
        data["arrived"] = arrived
    return Observation(seq, device, kind, float(at), data)


def engine(rows):
    return YardEngine(rows, DEVICES, POLICY)


def move(seq, at, start, target, unit, present=True, **extra):
    return obs(seq, "gate", "move", at, present=present, unit=unit,
               **{"from": start, "to": target}, **extra)


class OccupancyTest(unittest.TestCase):
    def test_cross_bay_tag_keeps_both_bays_conflicted(self):
        bench = engine([obs(1, "reader", "radio", 10, unit="GA-9", rssi={"N": -50, "S": -49})])
        for bay in ("N", "S"):
            state = bench.view(bay)
            self.assertEqual(state["status"], "conflict")
            self.assertFalse(state["identified"])

    def test_pressure_proves_occupancy_but_not_identity(self):
        bench = engine([obs(1, "loop-N", "pressure", 0, bay="N", present=True, source_epoch=1)])
        state = bench.view("N")
        self.assertEqual(state["status"], "occupied")
        self.assertFalse(state["identified"])
        self.assertIsNone(state["unit"])

    def test_missing_reader_identity_falls_back_to_gate(self):
        bench = engine([
            obs(1, "loop-N", "pressure", 0, bay="N", present=True, source_epoch=1),
            move(2, 2, "yard", "N", "GA-1"),
        ])
        state = bench.view("N")
        self.assertEqual((state["status"], state["unit"]), ("occupied", "GA-1"))

    def test_stuck_high_after_departure_preserves_conflict(self):
        bench = engine([
            obs(1, "loop-N", "pressure", 0, bay="N", present=True, source_epoch=1),
            move(2, 2, "yard", "N", "GA-1"),
            move(3, 5, "N", "S", "GA-1"),
            obs(4, "loop-S", "pressure", 5, bay="S", present=True, source_epoch=1),
            obs(5, "loop-N", "fault", 5, bay="N", status="stuck-high", source_epoch=1),
        ])
        state = bench.view("N")
        self.assertEqual(state["status"], "conflict")
        self.assertIn("卡高", state["conflict"])
        self.assertIn("离位", state["conflict"])

    def test_faulted_source_alone_is_not_evidence(self):
        bench = engine([obs(1, "loop-N", "fault", 1, bay="N", status="stuck-high", source_epoch=1)])
        self.assertNotEqual(bench.view("N")["status"], "occupied")
        self.assertNotEqual(bench.view("N")["status"], "empty")

    def test_departure_needs_no_reader_to_clear_when_lead_agrees(self):
        bench = engine([
            move(1, 1, "yard", "N", "GA-1"),
            move(2, 5, "N", "S", "GA-1"),
            obs(3, "reader", "radio", 6, unit="GA-1", rssi={"N": -49, "S": -51}, source_epoch=1),
            obs(4, "lead", "site_check", 12, **{"from": 6, "until": 20,
                                               "claims": {"N": {"empty": True}}}),
        ])
        state = bench.view("N")
        self.assertEqual(state["status"], "empty")
        self.assertEqual(state["basis"], "manual_absence")

    def test_reader_crossing_both_bays_blocks_departure_release(self):
        bench = engine([
            move(1, 2, "yard", "N", "GA-1"),
            obs(2, "reader", "radio", 3, unit="GA-1", rssi={"N": -50}, source_epoch=1),
            move(3, 5, "N", "S", "GA-1"),
            obs(4, "reader", "radio", 6, unit="GA-1", rssi={"N": -48, "S": -50}, source_epoch=2),
        ])
        self.assertEqual(bench.view("N")["status"], "conflict")

    def test_low_pressure_alone_cannot_prove_empty(self):
        bench = engine([
            obs(1, "loop-N", "pressure", 0, bay="N", present=True, source_epoch=1),
            obs(2, "loop-N", "pressure", 3, bay="N", present=False, source_epoch=1),
        ])
        state = bench.view("N")
        self.assertEqual(state["status"], "unknown")
        self.assertIn("独立", state["note"])

    def test_late_old_epoch_is_history_only(self):
        bench = engine([
            obs(1, "loop-N", "pressure", 0, bay="N", present=True, source_epoch=1),
            obs(2, "loop-N", "pressure", 1, bay="N", present=True, source_epoch=2),
            obs(3, "loop-N", "pressure", 0.5, arrived=9, bay="N", present=False, source_epoch=1),
        ])
        self.assertEqual(bench.view("N")["status"], "occupied")
        self.assertEqual([item["seq"] for item in bench.rejected], [3])

    def test_two_vehicles_same_bay_is_conflict_not_merge(self):
        bench = engine([
            move(1, 1, "yard", "N", "GA-1"),
            move(2, 3, "yard", "N", "GA-2"),
        ])
        state = bench.view("N")
        self.assertEqual(state["status"], "conflict")
        self.assertIn("GA-1", state["conflict"])
        self.assertIn("GA-2", state["conflict"])

    def test_bond_elsewhere_proves_old_bay_gone(self):
        bench = engine([
            move(1, 1, "yard", "N", "GA-1"),
            obs(2, "ground-S", "bond", 4, bay="S", unit="GA-1", ok=True),
        ])
        self.assertEqual(bench.view("N")["status"], "empty")

    def test_unidentified_departure_is_not_absence_proof(self):
        bench = engine([
            move(1, 1, "yard", "N", "GA-1"),
            move(2, 5, "N", "S", None),
        ])
        state = bench.view("N")
        self.assertEqual(state["status"], "occupied")
        self.assertEqual(state["unit"], "GA-1")
        self.assertTrue(any(item["kind"] == "unidentified_departure"
                            for item in state["conflicts"]))

    def test_lead_can_name_vehicle_while_pressure_corroborates(self):
        bench = engine([
            obs(1, "loop-N", "pressure", 0, bay="N", present=True, source_epoch=1),
            obs(2, "lead", "site_check", 3, **{"from": 0, "until": 10,
                                               "claims": {"N": {"occupied": True, "unit": "GA-7"}}}),
        ])
        state = bench.view("N")
        self.assertEqual((state["status"], state["unit"]), ("occupied", "GA-7"))

    def test_unknown_second_arrival_keeps_bay_unresolved(self):
        bench = engine([
            move(1, 1, "yard", "N", "GA-1"),
            move(2, 5, "yard", "N", None),
        ])
        state = bench.view("N")
        self.assertEqual(state["status"], "occupied")
        self.assertTrue(any(item["kind"] == "unidentified_arrival"
                            for item in state["conflicts"]))

    def test_old_handover_note_cannot_merge_two_vehicles(self):
        # 交接清单的时段在第二台车进入之前；它不能把之后的两台车合并成一笔。
        bench = engine([
            move(1, 1, "yard", "N", "GA-1"),
            obs(2, "lead", "site_check", 2, **{"from": 0, "until": 3,
                                               "claims": {"N": {"empty": True, "clear_surface": True}}}),
            move(3, 4, "yard", "N", "GA-2"),
            move(4, 6, "yard", "N", None),
        ])
        state = bench.view("N", at=8)
        self.assertEqual(state["status"], "conflict")
        self.assertIn("GA-1", state["conflict"])
        self.assertIn("GA-2", state["conflict"])
        self.assertFalse(bench.check("N", "sweep", at=8)["ok"])

    def test_weak_evidence_does_not_clear_established_occupancy(self):
        bench = engine([
            move(1, 1, "yard", "N", "GA-1"),
            obs(2, "reader", "radio", 10, unit="GA-1", rssi={"N": -50, "S": -50}, source_epoch=1),
        ])
        self.assertEqual(bench.view("N")["status"], "occupied")


class AdmissionTest(unittest.TestCase):
    def test_inspection_requires_vehicle_and_bond(self):
        bench = engine([move(1, 1, "yard", "N", "GA-1")])
        verdict = bench.check("N", "inspection", "GA-1")
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["checks"][-1]["need"], "bond")

    def test_inspection_with_vehicle_and_bond_is_admitted(self):
        bench = engine([
            move(1, 1, "yard", "N", "GA-1"),
            obs(2, "ground-N", "bond", 2, bay="N", unit="GA-1", ok=True),
        ])
        self.assertTrue(bench.check("N", "inspection", "GA-1")["ok"])

    def test_sweep_requires_empty_and_cleared_surface(self):
        bench = engine([
            move(1, 1, "yard", "N", "GA-1"),
            move(2, 5, "N", "S", "GA-1"),
            obs(3, "lead", "site_check", 9, **{"from": 6, "until": 20,
                                              "claims": {"N": {"empty": True, "clear_surface": True}}}),
        ])
        verdict = bench.check("N", "sweep")
        self.assertTrue(verdict["ok"])
        self.assertEqual({item["need"] for item in verdict["checks"]}, {"empty", "clear_surface"})

    def test_sweep_without_cleared_surface_is_held(self):
        bench = engine([
            move(1, 1, "yard", "N", "GA-1"),
            move(2, 5, "N", "S", "GA-1"),
        ])
        self.assertFalse(bench.check("N", "sweep")["ok"])

    def test_possible_entry_holds_new_application(self):
        bench = engine([
            move(1, 2, "yard", "N", "GA-1"),
            move(2, 4, "yard", "N", "GA-2", present=False),
        ])
        bench.ingest(obs(3, "desk", "work", 5, id="J1", bay="N", type="inspection",
                         unit="GA-1", stage="start"))
        self.assertEqual(bench.jobs["J1"].status, "held")
        self.assertIn("正在进入", bench.jobs["J1"].history[-1]["reason"])

    def test_started_job_is_paused_when_entry_becomes_possible(self):
        bench = engine([move(1, 1, "yard", "N", "GA-1"),
                        obs(2, "ground-N", "bond", 2, bay="N", unit="GA-1", ok=True)])
        bench.ingest(obs(3, "desk", "work", 3, id="J1", bay="N", type="inspection",
                         unit="GA-1", stage="start"))
        self.assertEqual(bench.jobs["J1"].status, "active")
        bench.ingest(move(4, 4, "yard", "N", "GA-2", present=False))
        self.assertEqual(bench.jobs["J1"].status, "pending_review")

    def test_lost_sensor_does_not_invalidate_remaining_evidence(self):
        bench = engine([
            move(1, 1, "yard", "N", "GA-1"),
            obs(2, "ground-N", "bond", 2, bay="N", unit="GA-1", ok=True),
            obs(3, "desk", "work", 3, id="I", bay="N", type="inspection",
                unit="GA-1", stage="start"),
            obs(4, "loop-N", "fault", 4, bay="N", status="stuck-high", source_epoch=1),
        ])
        self.assertEqual(bench.jobs["I"].status, "active")
        self.assertEqual(bench.job_view(bench.jobs["I"])["continuation"]["decision"], "continue")

    def test_broken_bond_pauses_inspection(self):
        bench = engine([
            move(1, 1, "yard", "N", "GA-1"),
            obs(2, "ground-N", "bond", 2, bay="N", unit="GA-1", ok=True),
            obs(3, "desk", "work", 3, id="I", bay="N", type="inspection",
                unit="GA-1", stage="start"),
            obs(4, "ground-N", "bond", 5, bay="N", unit="GA-1", ok=False),
        ])
        self.assertEqual(bench.jobs["I"].status, "pending_review")
        self.assertEqual(bench.job_view(bench.jobs["I"])["continuation"]["decision"], "pause")


class ExtensionTest(unittest.TestCase):
    def test_new_bay_is_configuration_only(self):
        devices = json.loads(json.dumps(DEVICES))
        policy = json.loads(json.dumps(POLICY))
        devices["loop-E"] = {"observes": ["E"], "type": "pressure"}
        devices["gate"]["observes"].append("E")
        devices["reader"]["observes"].append("E")
        policy["bays"].append("E")
        policy["neighbors"]["E"] = ["N"]
        policy["neighbors"]["N"].append("E")
        bench = YardEngine([
            obs(1, "loop-E", "pressure", 0, bay="E", present=True, source_epoch=1),
            move(2, 2, "yard", "E", "GA-3"),
        ], devices, policy)
        self.assertEqual(bench.view("E")["unit"], "GA-3")
        self.assertEqual(bench.view("N")["status"], "unknown")


class ReviewTest(unittest.TestCase):
    def test_site_check_cannot_clear_unclaimed_bay(self):
        bench = engine([
            obs(1, "loop-N", "fault", 1, bay="N", status="stuck-high", source_epoch=1),
            obs(2, "lead", "site_check", 3, **{"from": 0, "until": 10,
                                               "claims": {"S": {"empty": True}}}),
        ])
        self.assertNotEqual(bench.view("N")["status"], "empty")
        self.assertEqual(bench.view("S")["status"], "empty")

    def test_site_check_resolves_affected_job(self):
        bench = engine([
            move(1, 1, "yard", "N", "GA-1"),
            move(2, 5, "N", "S", "GA-1"),
            obs(3, "desk", "work", 6, id="SW", bay="N", type="sweep", stage="start"),
            obs(4, "lead", "site_check", 9, **{"from": 7, "until": 20,
                                               "claims": {"N": {"empty": True, "clear_surface": True}}}),
        ])
        self.assertEqual(bench.jobs["SW"].status, "active")
        self.assertTrue(bench.jobs["SW"].history[-1]["to"] == "active")

    def test_site_check_window_expiry_removes_admission(self):
        bench = engine([
            move(1, 1, "yard", "N", "GA-1"),
            move(2, 5, "N", "S", "GA-1"),
            obs(3, "lead", "site_check", 9, **{"from": 7, "until": 12,
                                               "claims": {"N": {"empty": True,
                                                                "clear_surface": True}}}),
        ])
        self.assertTrue(bench.check("N", "sweep", at=11.5)["ok"])
        self.assertFalse(bench.check("N", "sweep", at=30)["ok"])

    def test_site_check_review_does_not_clear_fault_itself(self):
        bench = engine([
            obs(1, "loop-S", "fault", 1, bay="S", status="stuck-high", source_epoch=1),
            obs(2, "lead", "site_check", 4, **{"from": 0, "until": 10,
                                               "claims": {"N": {"empty": True}}}),
        ])
        self.assertEqual(bench.view("N")["status"], "empty")
        self.assertIn("loop-S", bench.view("S")["degraded"])

    def test_late_weak_reading_keeps_lead_verified_state(self):
        bench = engine([
            obs(1, "lead", "site_check", 5, **{"from": 0, "until": 20,
                                               "claims": {"N": {"empty": True}}}),
            obs(2, "reader", "radio", 8, unit="GA-9", rssi={"N": -50, "S": -51},
                source_epoch=1),
        ])
        state = bench.view("N")
        self.assertEqual(state["status"], "empty")
        self.assertTrue(any(item["kind"] == "weak_vs_manual" for item in state["conflicts"]))

    def test_shift_check_lists_degraded_sources(self):
        service = IngestService([
            obs(1, "loop-N", "fault", 1, bay="N", status="stuck-high", source_epoch=1),
        ], DEVICES, POLICY)
        service.catch_up()
        shift = service.shift_check()
        self.assertEqual(shift["degraded_sources"][0]["device"], "loop-N")


class ReplayTest(unittest.TestCase):
    def test_recorded_scene_end_state(self):
        rows = load_scene()
        bench = engine(rows)
        north = bench.view("N")
        south = bench.view("S")
        self.assertEqual((north["status"], north["unit"]), ("occupied", "GA-2"))
        self.assertEqual((south["status"], south["unit"]), ("occupied", "GA-1"))
        self.assertIn("loop-N", north["degraded"])
        self.assertEqual(bench.jobs["C-18"].status, "suspended")
        self.assertEqual(bench.jobs["C-20"].status, "suspended")
        self.assertEqual(bench.jobs["C-22"].status, "held")

    def test_recorded_scene_transitions_are_explained(self):
        bench = engine(load_scene())
        jobs = {job_id: [entry["to"] for entry in job.history]
                for job_id, job in bench.jobs.items()}
        self.assertEqual(jobs["C-20"], ["held", "active", "suspended"])
        self.assertIn("active", jobs["C-18"])


class ServiceTest(unittest.TestCase):
    def test_http_round_trip(self):
        service = IngestService(load_scene(), DEVICES, POLICY)
        with make_server(service, 0) as server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(base + "/devices", timeout=4) as reply:
                    self.assertEqual(json.load(reply), DEVICES)
                body = json.dumps({"count": 14}).encode()
                with urlopen(Request(base + "/ingest/advance", data=body), timeout=4) as reply:
                    self.assertEqual(len(json.load(reply)["ingested"]), 14)
                with urlopen(base + "/bays", timeout=4) as reply:
                    bays = json.load(reply)
                    self.assertEqual(bays["N"]["status"], "empty")
                with urlopen(base + "/shift", timeout=4) as reply:
                    self.assertEqual(json.load(reply)["bays"]["S"]["unit"], "GA-1")
                with urlopen(base + "/check?bay=N&type=sweep", timeout=4) as reply:
                    self.assertTrue(json.load(reply)["ok"])
                with urlopen(base + "/explain?bay=N", timeout=4) as reply:
                    self.assertIn("timeline", json.load(reply))
                message = json.dumps({"device": "gate", "id": "probe-1",
                                      "action": "probe"}).encode()
                with urlopen(Request(base + "/commands", data=message), timeout=4) as reply:
                    self.assertEqual(reply.status, 202)
                    self.assertEqual(json.load(reply)["receipt"], "transport-only")
            finally:
                server.shutdown()
                worker.join()

    def test_ingest_single_observation(self):
        service = IngestService([], DEVICES, POLICY)
        record = {"seq": 1, "device": "loop-N", "kind": "pressure", "at": 0,
                  "bay": "N", "present": True, "source_epoch": 1}
        result = service.ingest_mapping(dict(record))
        self.assertTrue(result["accepted"])
        self.assertEqual(service.bays()["N"]["status"], "occupied")


if __name__ == "__main__":
    unittest.main()
