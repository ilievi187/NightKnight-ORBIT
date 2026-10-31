"""Plan Mode kernel unit tests (issue #11 contract, driven with injected clocks)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orbit.fleet import Fleet, FleetStore
from orbit.mission_api import create_worker_hooks, demo_mission_spec, demo_root
from orbit.planmode import (
    DEFAULT_MAX_ATTEMPTS,
    PlanError,
    PlanStore,
    Supervisor,
    Verifier,
    parse_criteria,
)


class TemporaryWorkspace(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orbit-plan-"))
        self.database = self.tmp / "orbit.sqlite3"
        self.workers_root = self.tmp / "workspace"
        self.demo = self.workers_root / "orbit-demo"
        self.demo.mkdir(parents=True)
        self.fleet = Fleet(FleetStore(self.database), heartbeat_ttl=20.0,
                           running_task_ids=create_worker_hooks(self.database)[0])
        self.supervisor = Supervisor(PlanStore(self.database), default_root=self.workers_root,
                                     fleet=self.fleet, heartbeat_ttl=20.0)
        self.now = 1_800_000_000.0

    def register_worker(self, name="MockA", caps=("orbit.plan.deliver",), token_holder=None):
        pairing = self.fleet.create_pairing(now=self.now)
        result = self.fleet.register(pairing["pairing_code"], name, "nightknight", "mock",
                                     list(caps), version="0.1", now=self.now)
        token = result["worker_token"]
        self.fleet.heartbeat(result["worker_id"], now=self.now + 1)
        if token_holder is not None:
            token_holder.append(token)
        return result

    def grant(self, denied=("orbit-demo/secret.md",)):
        from orbit.planmode import MissionGrant
        return MissionGrant(root=self.demo, denied_paths=denied)


class ParseCriteriaTests(TemporaryWorkspace):
    def test_requires_criteria(self):
        with self.assertRaises(PlanError):
            parse_criteria([])
        with self.assertRaises(PlanError):
            parse_criteria("nope")

    def test_validates_kinds_and_paths(self):
        with self.assertRaises(PlanError):
            parse_criteria([{"kind": "chocolate", "path": "x"}])
        with self.assertRaises(PlanError):
            parse_criteria([{"kind": "file_exists", "path": "../escape"}])
        with self.assertRaises(PlanError):
            parse_criteria([{"kind": "file_contains", "path": "x.txt", "needle": "  "}])
        ok = parse_criteria([{"kind": "sha256", "path": "x", "sha256": "f" * 64}])
        self.assertEqual(ok[0].expected_sha256, "f" * 64)

    def test_contains_requires_needle(self):
        criteria = parse_criteria([{"kind": "file_contains", "path": "a.md", "needle": "VERIFIED"}])
        self.assertEqual(criteria[0].needle, "VERIFIED")


class VerifierTests(TemporaryWorkspace):
    def test_missing_file_fails_honestly(self):
        criterion = parse_criteria([{"kind": "file_exists", "path": "nope.txt"}])[0]
        ok, message = self.supervisor.verifier.check(criterion, self.demo)
        self.assertFalse(ok)
        self.assertIn("MISSING", message)

    def test_contains_passes_only_on_content(self):
        (self.demo / "a.md").write_text("# report\nVERIFIED body", encoding="utf-8")
        good = parse_criteria([{"kind": "file_contains", "path": "a.md", "needle": "VERIFIED"}])[0]
        ok, message = self.supervisor.verifier.check(good, self.demo)
        self.assertTrue(ok); self.assertIn("CONTAINS", message)
        bad = parse_criteria([{"kind": "file_contains", "path": "a.md", "needle": "FAILED"}])[0]
        ok, message = self.supervisor.verifier.check(bad, self.demo)
        self.assertFalse(ok); self.assertIn("NOT SATISFIED", message)

    def test_path_traversal_rejected(self):
        from orbit.planmode import Criterion, new_id
        eviller = Criterion(cid="crit-x", text="escape", kind="file_exists", path="../../etc/passwd")
        ok, message = self.supervisor.verifier.check(eviller, self.demo)
        self.assertFalse(ok); self.assertIn("REJECTED", message)

    def test_symlink_escape_rejected(self):
        from orbit.planmode import new_id
        outside = self.tmp / "outside.txt"
        outside.write_text("Leak", encoding="utf-8")
        link = self.demo / "leak.txt"
        link.symlink_to(outside)
        eviller = parse_criteria([{"kind": "file_exists", "path": "leak.txt"}])[0]
        try:
            ok, message = self.supervisor.verifier.check(eviller, self.demo)
        except (PlanError, OSError):
            return  # linux raises on resolved-link check in some sandboxes
        self.assertFalse(ok, "symlink escaping the grant must not verify")

    def test_sha256(self):
        blob = self.demo / "x"
        blob.write_bytes(b"payload")
        import hashlib
        digest = hashlib.sha256(b"payload").hexdigest()
        as_hex = parse_criteria([{"kind": "sha256", "path": "x", "sha256": digest}])[0]
        ok, message = self.supervisor.verifier.check(as_hex, self.demo)
        self.assertTrue(ok, message)
        wrong = parse_criteria([{"kind": "sha256", "path": "x", "sha256": "a" * 64}])[0]
        ok, message = self.supervisor.verifier.check(wrong, self.demo)
        self.assertFalse(ok); self.assertIn("MISMATCH", message)


class MissionLifecycleTests(TemporaryWorkspace):
    def demo_criteria(self):
        spec = demo_mission_spec(self.demo)
        return spec["criteria"]

    def test_demo_spec_shape(self):
        spec = demo_mission_spec(self.demo)
        self.assertEqual(len(spec["criteria"]), 4)
        self.assertEqual(spec["grant"]["denied_paths"], ["secret.md"])

    def test_creates_demo_root(self):
        ws = self.tmp / "another-workspace"
        ws.mkdir()
        root = demo_root(ws)
        self.assertEqual(root, (ws / "orbit-demo").resolve())
        self.assertTrue(root.is_dir())

    def test_mission_waits_for_worker(self):
        mission = self.supervisor.create_mission("demo goal", self.demo_criteria(),
                                                 grant=self.grant(), now=self.now)
        self.assertEqual(mission["stage"], "ASSIGN")
        waiting = [e["kind"] for e in mission["events"]]
        self.assertIn("wait", waiting)
        self.assertEqual(mission["tasks"][0]["status"], "PENDING")

    def test_full_loop_two_iterations(self):
        token_holder = []
        self.register_worker(token_holder=token_holder)
        mission = self.supervisor.create_mission("demo goal", self.demo_criteria(),
                                                 grant=self.grant(), now=self.now)
        mid = mission["id"]
        self.assertEqual(mission["stage"], "RUN", mission["events"][-1]["message"])
        worker = self.fleet.store.workers()[0]
        attempts = {}
        clock = [self.now]

        def beat():
            clock[0] += 2
            self.fleet.heartbeat(worker["id"], now=clock[0])

        def drive(task_path: str) -> bool:
            beat()
            view = self.supervisor.mission_view(mid)
            target = next((t for t in view["tasks"]
                           if t["spec"].get("path") == task_path and t["status"] in ("ASSIGNED", "RUNNING")), None)
            if target is None:
                return False
            if target["status"] == "ASSIGNED":
                self.supervisor.claim(target["id"], worker, now=clock[0])
            beat()
            broken = attempts.get(task_path, 0) == 0 and task_path.endswith("engine.py")
            attempts[task_path] = attempts.get(task_path, 0) + 1
            (self.demo / task_path).write_text(
                "# broken first attempt" if broken else self._good_content(task_path), encoding="utf-8")
            self.supervisor.report(target["id"], worker,
                                   {"artifacts": [task_path], "notes": "flaky" if broken else "fixed"},
                                   now=clock[0])
            return True

        # Attempt 1: engine broken on purpose, then README + report fine.
        self.assertTrue(drive("engine.py"))
        self.assertTrue(drive("report.md"))
        self.assertTrue(drive("README.md"))
        beat()
        self.supervisor.advance(mid, now=clock[0])
        mission = self.supervisor.mission_view(mid)
        self.assertEqual(mission["iteration"], 2, self._events(mission))
        self.assertFalse(mission["criteria"][0]["satisfied"])
        self.assertTrue(mission["criteria"][2]["satisfied"], mission["criteria"][2]["evidence"])
        retry = [t for t in mission["tasks"] if t["attempt"] == 2]
        self.assertEqual(len(retry), 1)

        # Attempt 2: correct engine artifact -> COMPLETE.
        self.assertTrue(drive("engine.py"))
        beat()
        self.supervisor.advance(mid, now=clock[0])
        mission = self.supervisor.mission_view(mid)
        self.assertEqual(mission["stage"], "COMPLETE", self._events(mission))
        self.assertTrue(all(c["satisfied"] for c in mission["criteria"]))
        self.assertTrue(any(t["status"] == "VERIFIED" and t["attempt"] == 2 for t in mission["tasks"]))
        self.assertEqual(attempts["engine.py"], 2)  # broken once + fixed once

    def _good_content(self, path):
        base = "# ORBIT demo artifact\n"
        if path == "engine.py":
            return base + "/* engine */ class DemoEngine:\n    def run(self): pass\n"
        if path == "report.md":
            return base + "VERIFIED by evidence\n"
        if path == "README.md":
            return base + "mentions DemoEngine\n"
        return base

    def _events(self, mission):
        return "; ".join(f"{e['stage']}:{e['message'][:60]}" for e in list(reversed(mission["events"]))[:6])

    def test_scoped_denial_blocks_only_dependent_task(self):
        self.register_worker()
        mission = self.supervisor.create_mission(
            "denial demo",
            [{"kind": "file_exists", "path": "simple.txt"}],
            grant=self.grant(),
            extra_specs=[("Write secret.md", {"op": "probe", "path": "orbit-demo/secret.md"})],
            now=self.now)
        self.assertEqual(mission["stage"], "RUN")
        statuses = {t["title"][:12]: t["status"] for t in mission["tasks"]}
        self.assertEqual(list(statuses.values()).count("BLOCKED"), 1)
        self.assertEqual(list(statuses.values()).count("ASSIGNED"), 1)
        denied = self.supervisor.mission_view(mission["id"])["tasks"]
        self.assertTrue(any("GRANT DENIED" in (t["result_summary"] or "") for t in denied))

    def test_worker_lost_replans(self):
        token_holder = []
        self.register_worker(token_holder=token_holder)
        mission = self.supervisor.create_mission(
            "lost worker demo", [{"kind": "file_exists", "path": "artifact.txt"}],
            grant=self.grant(denied=()), now=self.now)
        task = [t for t in mission["tasks"] if t["status"] == "ASSIGNED"][0]
        worker = self.fleet.store.workers()[0]
        self.supervisor.claim(task["id"], worker, now=self.now + 2)
        # simulate heartbeat TTL expiry without tick() semantics drift
        later = self.now + 2 + self.fleet.heartbeat_ttl + 5
        self.fleet.age(now=later)
        self.supervisor.advance(mission["id"], now=later)
        mission = self.supervisor.mission_view(mission["id"])
        lost = [t for t in mission["tasks"] if t["status"] == "FAILED"]
        retry = [t for t in mission["tasks"] if t["attempt"] == 2 and t["status"] in ("PENDING", "ASSIGNED")]
        self.assertEqual(len(lost), 1, self._events(mission))
        self.assertEqual(len(retry), 1)
        self.assertTrue(any(e["kind"] == "worker-lost" for e in mission["events"]))

    def test_max_iteration_guard_blocks(self):
        self.register_worker()
        criteria = [{"kind": "file_contains", "path": "grim.txt", "needle": "IMPOSSIBLE"}]
        mission = self.supervisor.create_mission("guard test", criteria, grant=self.grant(denied=()),
                                                 max_iterations=2, now=self.now)
        mid = mission["id"]
        worker = self.fleet.store.workers()[0]
        for attempt in range(6):
            tasks = [t for t in self.supervisor.mission_view(mid)["tasks"] if t["status"] == "ASSIGNED"]
            if not tasks:
                break
            self.supervisor.claim(tasks[0]["id"], worker, now=self.now + attempt * 3 + 2)
            (self.demo / "grim.txt").write_text("wrong content", encoding="utf-8")
            self.supervisor.report(tasks[0]["id"], worker,
                                   {"artifacts": ["grim.txt"], "notes": "again"}, now=self.now + attempt * 3 + 3)
            self.supervisor.advance(mid, now=self.now + attempt * 3 + 4)
            self.supervisor.advance(mid, now=self.now + attempt * 3 + 5)
            if self.supervisor.store.mission(mid)["stage"] in ("BLOCKED", "COMPLETE"):
                break
        mission = self.supervisor.mission_view(mid)
        self.assertEqual(mission["stage"], "BLOCKED", self._events(mission))
        self.assertFalse(mission["criteria"][0]["satisfied"])
        self.assertIn("BLOCKED", mission["blocked_note"])

    def test_cancel(self):
        mission = self.supervisor.create_mission("cancel me", [{"kind": "file_exists", "path": "x"}],
                                                 grant=self.grant(denied=()), now=self.now)
        view = self.supervisor.cancel(mission["id"], note="user requested", now=self.now + 1)
        self.assertEqual(view["stage"], "CANCELLED")

    def test_persistence_reopen(self):
        token_holder = []
        self.register_worker(token_holder=token_holder)
        mission = self.supervisor.create_mission("persist test", [{"kind": "file_exists", "path": "p.txt"}],
                                                 grant=self.grant(denied=()), now=self.now)
        mid = mission["id"]
        reopened = Supervisor(PlanStore(self.database), default_root=self.workers_root,
                              fleet=self.fleet, heartbeat_ttl=20.0)
        view = reopened.mission_view(mid)
        self.assertEqual(view["goal"], "persist test")
        (self.demo / "p.txt").write_text("done", encoding="utf-8")
        self.supervisor.tick(now=self.now + 20)
        worker = self.fleet.store.workers()[0]
        task = [t for t in self.supervisor.mission_view(mid)["tasks"] if t["status"] == "ASSIGNED"]
        if task:
            self.supervisor.claim(task[0]["id"], worker, now=self.now + 21)
            self.supervisor.report(task[0]["id"], worker, {"artifacts": ["p.txt"]}, now=self.now + 22)
        self.supervisor.tick(now=self.now + 23)
        mission = self.supervisor.mission_view(mid)
        self.assertEqual(mission["stage"], "COMPLETE", self._events(mission))

    def test_tick_survives_and_advances(self):
        token_holder = []
        self.register_worker(token_holder=token_holder)
        mission = self.supervisor.create_mission("tick test", [{"kind": "file_exists", "path": "t.txt"}],
                                                 grant=self.grant(denied=()), now=self.now)
        self.fleet.heartbeat(self.fleet.store.workers()[0]["id"], now=self.now + 1)
        (self.demo / "t.txt").write_text("ok", encoding="utf-8")
        self.supervisor.tick(now=self.now + 2)
        self.supervisor.tick(now=self.now + 3)
        worker = self.fleet.store.workers()[0]
        self.fleet.heartbeat(worker["id"], now=self.now + 4)
        self.supervisor.tick(now=self.now + 5)
        worker = self.fleet.store.workers()[0]
        task = [t for t in self.supervisor.mission_view(mission["id"])["tasks"] if t["status"] == "ASSIGNED"]
        if task:
            self.supervisor.claim(task[0]["id"], worker, now=self.now + 6)
            self.supervisor.report(task[0]["id"], worker, {"artifacts": ["t.txt"]}, now=self.now + 7)
        self.supervisor.tick(now=self.now + 8)
        mission = self.supervisor.mission_view(mission["id"])
        self.assertEqual(mission["stage"], "COMPLETE", self._events(mission))


if __name__ == "__main__":
    unittest.main()
