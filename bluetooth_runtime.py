"""Persistent Bluetooth event loop for the HTTP server.

All BLE objects stay on one event loop. Physical commands are serialized by
the HTTP server; failed commands are never replayed by this runtime.
"""

import asyncio
import logging
import threading
from pathlib import Path


class DeviceError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def read_config(path):
    try:
        values = {}
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                name, value = line.split("=", 1)
                values[name] = value.strip().strip("\"'")
        return values
    except (OSError, ValueError) as exc:
        raise DeviceError("device configuration unavailable", 503) from exc


class BluetoothBackend:
    def __init__(self, base_dir):
        # Import once, on the BLE thread, rather than on every HTTP request.
        from bleak import BleakScanner
        from bleak_retry_connector import get_device
        from switchbot.adv_parser import parse_advertisement_data
        from switchbot.const import SwitchbotModel
        from switchbot.devices.bot import Switchbot
        from switchbot.devices.lock import SwitchbotLock

        self.scanner = BleakScanner
        self.known_device = get_device
        self.parse_advertisement = parse_advertisement_data
        self.bot_class = Switchbot
        self.lock_class = SwitchbotLock
        self.lock_model = SwitchbotModel.LOCK_PRO
        self.base_dir = Path(base_dir)
        self.devices = {}

    async def _discard(self, kind):
        cached = self.devices.pop(kind, None)
        if cached:
            # This private API belongs to the vendored PySwitchbot version.
            try:
                async with asyncio.timeout(3):
                    await cached[1]._execute_forced_disconnect()
            except Exception:
                logging.getLogger(__name__).warning("Bluetooth cleanup failed for %s", kind)

    async def close(self):
        for kind in tuple(self.devices):
            await self._discard(kind)

    async def _device(self, kind, config):
        cached = self.devices.get(kind)
        if cached and cached[0] == config:
            # An unexpected disconnect can leave the encryption session in
            # memory. Reset it before reconnecting, just as a fresh CLI would.
            client = cached[1]._client
            if client is None or not client.is_connected:
                await cached[1]._execute_forced_disconnect()
            return cached[1]
        await self._discard(kind)
        if kind == "bot":
            mac = config["BOT_MAC"].upper()
            attempts = int(config.get("BOT_SCAN_ATTEMPTS", "3"))
            timeout = float(config.get("BOT_SCAN_TIMEOUT", "10"))
            if attempts < 1 or timeout <= 0:
                raise ValueError("invalid discovery settings")
        else:
            mac, attempts, timeout = config["LOCK_MAC"].upper(), 1, 3.0
        # The configured MAC already identifies the target. On Linux, reuse
        # BlueZ's Device1 object without starting discovery. Passing a plain
        # MAC to BleakClient would silently start a scan instead.
        ble_device = await self.known_device(mac)
        if ble_device is None:
            # BlueZ can forget unpaired devices after an adapter restart.
            # Discovery is only needed when no OS-level device object exists.
            for attempt in range(attempts):
                ble_device = await self.scanner.find_device_by_address(mac, timeout=timeout)
                if ble_device is not None:
                    break
                if attempt + 1 < attempts:
                    await asyncio.sleep(1)
        if ble_device is None:
            raise DeviceError(f"{kind} not found via Bluetooth")
        if kind == "bot":
            device = self.bot_class(ble_device, password=config["BOT_PASSWORD"], retry_count=3)
        else:
            device = self.lock_class(
                ble_device, config["LOCK_KEY_ID"], config["LOCK_ENC_KEY"],
                model=self.lock_model, scan_timeout=1,
            )
        self.devices[kind] = (config, device)
        return device

    async def _status(self, config):
        # A connected lock may stop advertising. Release it before requesting
        # a fresh advertisement; never return a previously cached door status.
        await self._discard("lock")
        mac = config["LOCK_MAC"].upper()
        result = asyncio.get_running_loop().create_future()

        def detected(device, advertisement):
            if device.address.upper() != mac or result.done():
                return
            parsed = self.parse_advertisement(device, advertisement)
            if parsed and parsed.data.get("model") in ("o", "$") and parsed.data.get("data"):
                result.set_result(str(parsed.data["data"]))

        async with self.scanner(detection_callback=detected):
            try:
                async with asyncio.timeout(5):
                    return await result
            except TimeoutError as exc:
                raise DeviceError("Lock Pro not found via Bluetooth") from exc

    async def execute(self, path):
        kind = "bot" if path == "/bld_door" else "lock"
        try:
            config = read_config(self.base_dir / f".switchbot-{kind}.env")
            if path == "/apt_door/status":
                return await self._status(config)
            device = await self._device(kind, config)
            if kind == "bot":
                # Warm up new/reopened GATT connections as on the Pi's CLI.
                # A connection already confirmed by a previous operation does
                # not need another preflight read. The press still checks ACK.
                if device._client is None or not device._client.is_connected:
                    if await device.get_basic_info() is None:
                        raise DeviceError("SwitchBot Bot did not respond to the status request")
                result = await device.press()
            else:
                # A reused instance must not skip an action based on an old
                # status. Match the CLI's uncached state checks on every call.
                device._sb_adv_data = None
                action = path.rsplit("/", 1)[-1]
                method = {"lock": device.lock, "unlock": device.unlock_without_unlatch, "open": device.unlock}[action]
                result = await method()
            if result is not True:
                raise DeviceError("device did not acknowledge the command")
            return "pressed" if kind == "bot" else str(result)
        except (KeyError, ValueError) as exc:
            raise DeviceError(f"{kind} configuration unavailable", 503) from exc

    async def run(self, path, timeout):
        try:
            async with asyncio.timeout(timeout):
                return await self.execute(path)
        except BaseException:
            # Drop stale BlueZ objects after failures. Do not retry a physical
            # operation whose outcome may be unknown (especially Bot presses).
            await self._discard("bot" if path == "/bld_door" else "lock")
            raise


class BluetoothWorker:
    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.backend = None
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._serve, name="bluetooth", daemon=True)
        self.thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.run_until_complete(self.loop.shutdown_default_executor())
            self.loop.close()

    async def _run(self, path, timeout):
        if self.backend is None:
            self.backend = BluetoothBackend(self.base_dir)
        return await self.backend.run(path, timeout)

    def execute(self, path, timeout):
        return asyncio.run_coroutine_threadsafe(self._run(path, timeout), self.loop).result()

    def close(self):
        try:
            if self.backend:
                asyncio.run_coroutine_threadsafe(self.backend.close(), self.loop).result()
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join()
