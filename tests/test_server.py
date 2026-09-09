import http.client
import importlib.util
import json
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "switchbot-server.py"
SPEC = importlib.util.spec_from_file_location("switchbot_server", MODULE_PATH)
server_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server_module)


class UserStoreAndPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "users.json"
        self.store = server_module.UserStore(self.database)
        self.store.bootstrap_legacy_tokens("bearer1", "bearer2")
        self.user = self.store.authenticate("bearer2")
        self.monday_10 = datetime(2026, 9, 7, 10, 0, tzinfo=server_module.LISBON)

    def tearDown(self):
        self.temporary.cleanup()

    def test_legacy_tokens_are_migrated_as_hashes(self):
        raw = self.database.read_text()
        self.assertNotIn("bearer1", raw)
        self.assertNotIn("bearer2", raw)
        self.assertEqual("admin", self.store.authenticate("bearer1")["role"])
        self.assertEqual("user", self.store.authenticate("bearer2")["role"])
        self.assertIsNone(self.store.authenticate("invalid"))

    def test_admin_bypasses_schedule_distance_and_remote_restrictions(self):
        admin = self.store.authenticate("bearer1")
        admin.update(schedules=[], allow_remote=False)
        self.assertEqual((True, None), server_module.access_decision(admin, True, self.monday_10))

    def test_user_schedule_day_enabled_distance_and_remote_rules(self):
        base = dict(self.user, schedules=[{"allowed_days": ["monday"], "start_time": "09:00", "end_time": "17:00"}])
        self.assertEqual((True, None), server_module.access_decision(base, False, self.monday_10))
        self.assertEqual((False, "outside_allowed_schedule"), server_module.access_decision(base, False, self.monday_10.replace(hour=18)))
        self.assertEqual((False, "outside_allowed_schedule"), server_module.access_decision(base, False, self.monday_10.replace(day=8)))
        self.assertEqual((False, "user_disabled"), server_module.access_decision(dict(base, enabled=False), False, self.monday_10))
        self.assertEqual(100.0, server_module.LOCAL_DISTANCE_LIMIT_M)
        self.assertEqual((False, "remote_access_not_allowed"), server_module.access_decision(base, True, self.monday_10))
        self.assertEqual((True, None), server_module.access_decision(dict(base, allow_remote=True), True, self.monday_10))

    def test_overnight_schedule_uses_previous_allowed_day(self):
        user = dict(self.user, schedules=[{"allowed_days": ["monday"], "start_time": "22:00", "end_time": "06:00"}])
        tuesday_02 = datetime(2026, 9, 8, 2, 0, tzinfo=server_module.LISBON)
        self.assertEqual((True, None), server_module.access_decision(user, False, tuesday_02))

    def test_multiple_periods_allow_any_matching_period(self):
        user = dict(self.user, schedules=[
            {"allowed_days": ["monday"], "start_time": "08:00", "end_time": "12:00"},
            {"allowed_days": ["monday"], "start_time": "14:00", "end_time": "20:00"},
            {"allowed_days": ["saturday", "sunday"], "start_time": "10:00", "end_time": "14:00"},
        ])
        self.assertEqual((True, None), server_module.access_decision(user, False, self.monday_10))
        self.assertEqual((False, "outside_allowed_schedule"), server_module.access_decision(user, False, self.monday_10.replace(hour=13)))
        self.assertEqual((True, None), server_module.access_decision(user, False, self.monday_10.replace(hour=15)))

    def test_legacy_single_schedule_payload_is_normalised(self):
        created = self.store.create_user({"name": "Legacy", "allowed_days": ["monday"], "start_time": "08:00", "end_time": "12:00"})
        fetched = self.store.get_user(created["id"])
        self.assertEqual([{"allowed_days": ["monday"], "start_time": "08:00", "end_time": "12:00"}], fetched["schedules"])
        self.assertNotIn("allowed_days", fetched)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = server_module.UserStore(Path(self.temporary.name) / "users.json")
        self.store.bootstrap_legacy_tokens("bearer1", "bearer2")
        self.httpd = server_module.SwitchBotHTTPServer(("127.0.0.1", 0), server_module.Handler)
        self.httpd.user_store = self.store
        self.httpd.command_timeout = 1
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.command_patch = mock.patch.object(
            server_module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "done\n", ""),
        )
        self.command_run = self.command_patch.start()
        self.time_patch = mock.patch.object(
            server_module,
            "current_lisbon_time",
            return_value=datetime(2026, 9, 7, 10, 0, tzinfo=server_module.LISBON),
        )
        self.time_patch.start()

    def tearDown(self):
        self.time_patch.stop()
        self.command_patch.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def request(self, method, path, token=None, body=None, coordinates=None):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            body = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if coordinates is not None:
            headers["X-Latitude"] = str(coordinates[0])
            headers["X-Longitude"] = str(coordinates[1])
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw) if raw else None

    def test_capabilities_identifies_user_and_never_returns_token(self):
        status, payload = self.request("GET", "/access/capabilities?lat=38.7109387&lon=-9.1702945", "bearer2")
        self.assertEqual(200, status)
        self.assertEqual({"id": 2, "name": "User 2", "role": "user"}, payload["user"])
        self.assertNotIn("token", json.dumps(payload))
        self.assertTrue(payload["doors"]["APT"]["can_open"])

    def test_admin_can_operate_without_coordinates_and_invalid_token_is_rejected(self):
        status, _ = self.request("POST", "/apt_door/open", "bearer1")
        self.assertEqual(200, status)
        status, _ = self.request("POST", "/apt_door/open", "invalid")
        self.assertEqual(401, status)

    def test_user_cannot_access_admin_endpoints(self):
        status, payload = self.request("GET", "/admin/users", "bearer2")
        self.assertEqual(403, status)
        self.assertEqual("admin_required", payload["error"])

    def test_distance_limit_field_is_not_accepted_or_returned(self):
        status, payload = self.request("POST", "/admin/users", "bearer1", {"name": "No limit", "max_distance_m": 500})
        self.assertEqual(400, status)
        self.assertIn("max_distance_m", payload["error"])
        status, listed = self.request("GET", "/admin/users", "bearer1")
        self.assertEqual(200, status)
        self.assertNotIn("max_distance_m", json.dumps(listed))

    def test_admin_crud_returns_token_only_on_create(self):
        new_values = {"name": "Maria", "enabled": True, "allow_remote": True, "schedules": [
            {"allowed_days": ["monday", "friday"], "start_time": "08:30", "end_time": "12:00"},
            {"allowed_days": ["monday", "friday"], "start_time": "14:00", "end_time": "19:00"},
        ]}
        status, created = self.request("POST", "/admin/users", "bearer1", new_values)
        self.assertEqual(201, status)
        self.assertIn("token", created)
        user_id = created["id"]
        status, fetched = self.request("GET", f"/admin/users/{user_id}", "bearer1")
        self.assertEqual(200, status)
        self.assertNotIn("token", fetched)
        self.assertNotIn("token_hash", fetched)
        self.assertEqual(2, len(fetched["schedules"]))
        status, updated = self.request("PUT", f"/admin/users/{user_id}", "bearer1", {"enabled": False, "name": "Maria S."})
        self.assertEqual(200, status)
        self.assertFalse(updated["enabled"])
        status, listed = self.request("GET", "/admin/users", "bearer1")
        self.assertEqual(200, status)
        self.assertEqual({1, 2, user_id}, {user["id"] for user in listed["users"]})
        self.assertNotIn("token", json.dumps(listed))
        status, _ = self.request("DELETE", f"/admin/users/{user_id}", "bearer1")
        self.assertEqual(204, status)

    def test_real_operations_repeat_user_policy(self):
        self.store.update_user(2, {"allowed_days": ["tuesday"]})
        coordinates = (38.7109387, -9.1702945)
        for path in ("/apt_door/open", "/apt_door/unlock", "/apt_door/lock"):
            with self.subTest(path=path):
                status, payload = self.request("POST", path, "bearer2", coordinates=coordinates)
                self.assertEqual(403, status)
                self.assertEqual("outside_allowed_schedule", payload["reason"])
        self.command_run.assert_not_called()

    def test_disabled_user_and_remote_policy_are_enforced(self):
        self.store.update_user(2, {"enabled": False})
        status, payload = self.request("GET", "/access/capabilities?lat=38.7109387&lon=-9.1702945", "bearer2")
        self.assertEqual("user_disabled", payload["doors"]["APT"]["reason"])
        self.store.update_user(2, {"enabled": True, "allow_remote": False})
        status, payload = self.request("POST", "/apt_door/open", "bearer2", coordinates=(38.72, -9.17))
        self.assertEqual(403, status)
        self.assertEqual("remote_access_not_allowed", payload["reason"])


if __name__ == "__main__":
    unittest.main()
