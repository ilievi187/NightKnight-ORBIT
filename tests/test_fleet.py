"""Fleet registry + pairing tests (issue #12 requirements 5–8)."""
import tempfile
import time
import unittest
from pathlib import Path

from orbit.fleet import Fleet, FleetError, FleetStore


class FleetBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orbit-fleet-"))
        self.database = self.tmp / "fleet.sqlite3"
        self.fleet = Fleet(FleetStore(self.database), heartbeat_ttl=20.0, pairing_ttl=600.0)
        self.now = 1_800_000_000.0

    def register(self, name="MockA", caps=("orbit.plan.deliver",), kind="mock", now=None):
        pairing = self.fleet.create_pairing(now=self.now if now is None else now)
        return self.fleet.register(pairing["pairing_code"], name, "nightknight", kind,
                                   list(caps), version="0.1", now=self.now if now is None else now), pairing


class PairingTests(FleetBase):
    def test_pairing_code_masked_in_listings(self):
        self.register(now=self.now)
        listed = self.fleet.store.pairings_recent()
        self.assertEqual(len(listed), 1)
        entry = listed[0]
        self.assertNotIn("pairing_code", entry)
        self.assertTrue(entry["used"])

    def test_single_use(self):
        _, pairing = self.register()
        with self.assertRaises(FleetError):
            self.fleet.evaluate_pairing(pairing["pairing_code"])  # burned on use

    def test_expiry(self):
        self.fleet.create_pairing(now=self.now)
        # 11 minutes later
        with self.assertRaises(FleetError):
            self.fleet.register("stale", "X", "h", "mock", ["c"], now=self.now + 660)

    def test_unknown_code_no_worker_created(self):
        with self.assertRaises(FleetError):
            self.fleet.register("000000-000000-000000", "X", "h", "mock", ["c"], now=self.now)
        self.assertEqual(self.fleet.store.workers(), [])

    def test_revoked_workers_hidden_from_dashboard(self):
        result, _ = self.register()
        worker_id = result["worker_id"]
        self.fleet.heartbeat(worker_id, now=self.now + 1)
        self.assertTrue(self.fleet.revoke(worker_id, now=self.now + 2))
        self.assertEqual([w for w in self.fleet.public_workers(self.now + 3) if w["id"] == worker_id], [])
        # token now rejected
        self.assertIsNone(self.fleet.authorize(result["worker_token"]))


class RegistrationTests(FleetBase):
    def test_token_once_and_hashed_at_rest(self):
        result, _ = self.register()
        raw_row = self.fleet.store.workers()[0]
        self.assertTrue(raw_row["token_hash"] != result["worker_token"])
        self.assertEqual(len(result["worker_token"]), 43)  # token_urlsafe(32)

    def test_capability_validation(self):
        pairing = self.fleet.create_pairing(now=self.now)
        with self.assertRaises(FleetError):
            self.fleet.register(pairing["pairing_code"], "X", "h", "mock", ["bad cap"], now=self.now)
        with self.assertRaises(FleetError):
            self.fleet.register(pairing["pairing_code"], "1X", "h", "rootkit", ["c"], now=self.now)

    def test_status_lifecycle(self):
        result, _ = self.register()
        worker_id = result["worker_id"]
        w = self.fleet.worker(worker_id)
        self.assertEqual(w["status"], "OFFLINE")  # honest until first heartbeat
        self.fleet.heartbeat(worker_id, now=self.now + 1)
        self.assertEqual(self.fleet.worker(worker_id)["status"], "IDLE")
        self.fleet.refresh_status(set(), now=self.now + 2)
        self.assertEqual(self.fleet.worker(worker_id)["status"], "IDLE")

    def test_aging_to_offline(self):
        result, _ = self.register()
        self.fleet.heartbeat(result["worker_id"], now=self.now + 1)
        aged = self.fleet.age(now=self.now + 100)  # TTL 20s exceeded
        self.assertEqual(aged, [result["worker_id"]])
        self.assertEqual(self.fleet.worker(result["worker_id"])["status"], "OFFLINE")
        self.assertEqual(self.fleet.capable_workers("orbit.plan.deliver", self.now + 100), [])

    def test_capable_workers_require_fresh_heartbeat(self):
        result, _ = self.register()
        eligible = self.fleet.capable_workers("orbit.plan.deliver", self.now + 2)
        self.assertEqual(eligible, [])  # never heartbeated -> not eligible
        self.fleet.heartbeat(result["worker_id"], now=self.now + 2)
        self.assertEqual(len(self.fleet.capable_workers("orbit.plan.deliver", self.now + 3)), 1)


class RegistryHonestyTests(FleetBase):
    def test_zero_workers_renders_zero_workers(self):
        self.assertEqual(self.fleet.public_workers(self.now), [])
        self.assertEqual(self.fleet.capable_workers("orbit.plan.deliver", self.now), [])


if __name__ == "__main__":
    unittest.main()
