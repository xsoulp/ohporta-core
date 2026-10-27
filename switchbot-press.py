#!/usr/bin/env python3

import asyncio
import os
import sys
from pathlib import Path

from bleak import BleakScanner
from switchbot.devices.bot import Switchbot


DEFAULT_SCAN_ATTEMPTS = 3
DEFAULT_SCAN_TIMEOUT = 10.0
SCAN_RETRY_DELAY = 1.0


def load_config():
    path = Path(__file__).parent / ".switchbot-bot.env"

    for line in path.read_text().splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        name, value = line.split("=", 1)
        os.environ[name] = value.strip().strip("\"'")


async def main():
    load_config()

    mac = os.environ["BOT_MAC"].upper()
    password = os.environ["BOT_PASSWORD"]

    scan_attempts = int(os.environ.get("BOT_SCAN_ATTEMPTS", DEFAULT_SCAN_ATTEMPTS))
    scan_timeout = float(os.environ.get("BOT_SCAN_TIMEOUT", DEFAULT_SCAN_TIMEOUT))
    ble_device = None

    for attempt in range(1, scan_attempts + 1):
        ble_device = await BleakScanner.find_device_by_address(
            mac,
            timeout=scan_timeout,
        )

        if ble_device is not None:
            break

        print(
            f"SwitchBot Bot not found (attempt {attempt}/{scan_attempts})",
            file=sys.stderr,
        )

        if attempt < scan_attempts:
            await asyncio.sleep(SCAN_RETRY_DELAY)

    if ble_device is None:
        raise SystemExit(
            f"SwitchBot Bot not found via Bluetooth after {scan_attempts} attempts"
        )

    device = Switchbot(
        ble_device,
        password=password,
        retry_count=3,
    )

    try:
        # Warm up and validate the GATT connection before sending a physical
        # command.  On this Raspberry Pi, BlueZ occasionally aborts the first
        # connection attempt; PySwitchbot retries it and keeps the successful
        # connection open for the press below.
        info = await device.get_basic_info()
        if info is None:
            raise SystemExit("SwitchBot Bot did not respond to the status request")

        result = await device.press()
        if result is not True:
            raise SystemExit(f"SwitchBot Bot did not acknowledge the command: {result!r}")

        print("pressed")
    finally:
        # PySwitchbot does not expose a public disconnect method.  Closing the
        # connection explicitly prevents BlueZ from reporting the normal
        # shutdown as an unexpected disconnect when asyncio exits.
        await device._execute_forced_disconnect()


asyncio.run(main())
