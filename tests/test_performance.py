"""Regression checks for persistent BLE and concurrent HTTP, without hardware."""

import asyncio
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bluetooth_runtime import BluetoothBackend, BluetoothWorker, DeviceError


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ohporta_server", ROOT / "ohporta-server.py")
server_module = importlib.util.module_from_spec(spec)
with patch.dict(os.environ, {name: "0" for name in (
    "APT_LATITUDE", "APT_LONGITUDE", "BLD_LATITUDE", "BLD_LONGITUDE",
)}):
    spec.loader.exec_module(server_module)


def fake_device():
    device = SimpleNamespace(
        _client=None,
        _sb_adv_data={"old_status": "locked"},
        _execute_forced_disconnect=AsyncMock(),
        press=AsyncMock(return_value=True),
        lock=AsyncMock(return_value=True),
        unlock=AsyncMock(return_value=True),
        unlock_without_unlatch=AsyncMock(return_value=True),
    )

    async def basic_info():
        device._client = SimpleNamespace(is_connected=True)
        return {"battery": 90}

    async def action():
        device._client = SimpleNamespace(is_connected=True)
        return True

    device.get_basic_info = AsyncMock(side_effect=basic_info)
    for name in ("lock", "unlock", "unlock_without_unlatch"):
        setattr(device, name, AsyncMock(side_effect=action))
    return device


class BluetoothTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / ".switchbot-bot.env").write_text("BOT_MAC=AA\nBOT_PASSWORD=test\n")
        (self.root / ".switchbot-lock.env").write_text("LOCK_MAC=BB\nLOCK_KEY_ID=01\nLOCK_ENC_KEY=test\n")
        # Inject hardware boundaries, retaining the actual runtime logic.
        self.backend = object.__new__(BluetoothBackend)
        self.backend.base_dir = self.root
        self.backend.devices = {}
        self.backend.scanner = Mock()
        self.backend.known_device = AsyncMock(return_value=None)
        self.backend.scanner.find_device_by_address = AsyncMock(return_value=object())
        self.backend.bot_class = Mock(side_effect=lambda *a, **k: fake_device())
        self.backend.lock_class = Mock(side_effect=lambda *a, **k: fake_device())
        self.backend.lock_model = object()

    async def asyncTearDown(self):
        await self.backend.close()
        self.directory.cleanup()

    async def test_repeated_press_reuses_discovery_and_connection(self):
        for _ in range(2):
            self.assertEqual(await self.backend.run("/bld_door", 1), "pressed")
        self.backend.scanner.find_device_by_address.assert_awaited_once()
        self.backend.bot_class.assert_called_once()
        device = self.backend.devices["bot"][1]
        self.assertEqual(device.press.await_count, 2)
        self.assertEqual(device.get_basic_info.await_count, 1)
        device._execute_forced_disconnect.assert_not_awaited()

    async def test_known_bluez_devices_do_not_trigger_discovery(self):
        known = object()
        self.backend.known_device.return_value = known
        self.assertEqual(await self.backend.run("/bld_door", 1), "pressed")
        self.assertEqual(await self.backend.run("/apt_door/lock", 1), "True")
        self.backend.scanner.find_device_by_address.assert_not_awaited()
        self.assertEqual(self.backend.known_device.await_count, 2)
        self.assertIs(self.backend.bot_class.call_args.args[0], known)
        self.assertIs(self.backend.lock_class.call_args.args[0], known)

    async def test_lock_actions_do_not_trust_cached_status(self):
        for action in ("lock", "unlock", "open"):
            self.assertEqual(await self.backend.run(f"/apt_door/{action}", 1), "True")
        device = self.backend.devices["lock"][1]
        self.assertIsNone(device._sb_adv_data)
        device.lock.assert_awaited_once()
        device.unlock_without_unlatch.assert_awaited_once()
        device.unlock.assert_awaited_once()
        self.backend.scanner.find_device_by_address.assert_awaited_once()

    async def test_disconnected_lock_resets_encryption_before_reuse(self):
        await self.backend.run("/apt_door/lock", 1)
        device = self.backend.devices["lock"][1]
        device._client.is_connected = False
        await self.backend.run("/apt_door/open", 1)
        device._execute_forced_disconnect.assert_awaited_once()
        self.backend.lock_class.assert_called_once()

    async def test_reopened_bot_connection_is_warmed_up_again(self):
        await self.backend.run("/bld_door", 1)
        device = self.backend.devices["bot"][1]
        device._client.is_connected = False
        await self.backend.run("/bld_door", 1)
        self.assertEqual(device.get_basic_info.await_count, 2)
        device._execute_forced_disconnect.assert_awaited_once()

    async def test_config_change_discards_previous_device(self):
        await self.backend.run("/bld_door", 1)
        previous = self.backend.devices["bot"][1]
        (self.root / ".switchbot-bot.env").write_text("BOT_MAC=CC\nBOT_PASSWORD=new\n")
        await self.backend.run("/bld_door", 1)
        previous._execute_forced_disconnect.assert_awaited_once()
        self.assertEqual(self.backend.bot_class.call_count, 2)

    async def test_negative_ack_is_not_success_or_replayed(self):
        await self.backend.run("/bld_door", 1)
        device = self.backend.devices["bot"][1]
        device.press.reset_mock()
        device.press.return_value = False
        with self.assertRaises(DeviceError):
            await self.backend.run("/bld_door", 1)
        device.press.assert_awaited_once()
        self.assertNotIn("bot", self.backend.devices)
        device._execute_forced_disconnect.assert_awaited_once()

    async def test_timeout_cancels_command_and_evicts_device(self):
        await self.backend.run("/bld_door", 1)
        device = self.backend.devices["bot"][1]
        cancelled = asyncio.Event()

        async def blocked():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        device.press = AsyncMock(side_effect=blocked)
        with self.assertRaises(TimeoutError):
            await self.backend.run("/bld_door", .02)
        self.assertTrue(cancelled.is_set())
        self.assertNotIn("bot", self.backend.devices)
        device.press.assert_awaited_once()

    async def test_status_uses_first_fresh_valid_advertisement(self):
        await self.backend.run("/apt_door/lock", 1)
        previous = self.backend.devices["lock"][1]
        backend = self.backend
        backend.parse_advertisement = Mock(side_effect=[
            None, SimpleNamespace(data={"model": "$", "data": {"status": "locked"}}),
        ])

        class Scanner:
            async def __aenter__(self):
                callback = backend.scanner.call_args.kwargs["detection_callback"]
                callback(SimpleNamespace(address="unrelated"), object())
                callback(SimpleNamespace(address="BB"), object())
                callback(SimpleNamespace(address="BB"), object())

            async def __aexit__(self, *args):
                pass

        backend.scanner.return_value = Scanner()
        self.assertEqual(await backend.run("/apt_door/status", 1), "{'status': 'locked'}")
        previous._execute_forced_disconnect.assert_awaited_once()
        self.assertNotIn("lock", backend.devices)


class WorkerTests(unittest.TestCase):
    def test_event_loop_runs_timers_between_http_requests(self):
        worker = BluetoothWorker(ROOT)
        fired = threading.Event()
        loops = []

        async def run(path, timeout):
            loop = asyncio.get_running_loop()
            loops.append(loop)
            loop.call_later(.01, fired.set)
            return "pressed"

        worker.backend = SimpleNamespace(run=run, close=AsyncMock())
        try:
            self.assertEqual(worker.execute("/bld_door", 1), "pressed")
            self.assertTrue(fired.wait(1), "idle disconnect timers must keep running")
            worker.execute("/bld_door", 1)
            self.assertIs(loops[0], loops[1])
        finally:
            worker.close()
        self.assertFalse(worker.thread.is_alive())


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.server = server_module.SwitchBotHTTPServer(("127.0.0.1", 0), server_module.Handler)
        self.server.user_store = server_module.UserStore(root / "users.json")
        self.server.user_store.bootstrap_users("admin-test-token")
        self.server.history_store = server_module.AccessHistoryStore(root / "history.json")
        self.server.command_timeout = 1
        self.server.bluetooth_worker = Mock()
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, path, method="GET", token="admin-test-token"):
        connection = HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request(method, path, headers={"Authorization": f"Bearer {token}"})
            response = connection.getresponse()
            return response.status, json.loads(response.read() or "null")
        finally:
            connection.close()

    def test_health_and_permissions_remain_responsive_during_ble_command(self):
        entered, release = threading.Event(), threading.Event()

        def execute(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError
            return "pressed"

        self.server.bluetooth_worker.execute.side_effect = execute
        results = []
        command = threading.Thread(target=lambda: results.append(self.request("/bld_door", "POST")))
        command.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.request("/health"), (200, {"ok": True}))
            self.assertEqual(self.request("/access/capabilities?lat=0&lon=0")[0], 200)
            self.assertEqual(self.request("/apt_door/open", "POST"), (409, {"ok": False, "error": "device_busy"}))
            self.assertEqual(self.request("/bld_door", "POST", token="wrong")[0], 401)
        finally:
            release.set()
            command.join()
        self.assertEqual(results, [(200, {"ok": True, "output": "pressed"})])
        self.server.bluetooth_worker.execute.assert_called_once()
        self.assertEqual(len(self.server.history_store.list_entries(10)), 1)

    def test_timeout_does_not_record_success_and_releases_command_lock(self):
        self.server.bluetooth_worker.execute.side_effect = TimeoutError
        self.assertEqual(self.request("/bld_door", "POST")[0], 504)
        self.assertEqual(self.server.history_store.list_entries(10), [])
        self.server.bluetooth_worker.execute.side_effect = None
        self.server.bluetooth_worker.execute.return_value = "pressed"
        self.assertEqual(self.request("/bld_door", "POST")[0], 200)

    def test_subprocess_fallback_preserves_output(self):
        self.server.bluetooth_worker = None
        with patch.object(server_module.subprocess, "run", return_value=SimpleNamespace(
            stdout="pressed\n", stderr="", returncode=0,
        )) as run:
            self.assertEqual(self.request("/bld_door", "POST"), (200, {"ok": True, "output": "pressed"}))
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
