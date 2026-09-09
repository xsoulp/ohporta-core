#!/home/xsoulp/switchbot-venv/bin/python

import asyncio
import os
import sys
from pathlib import Path

from bleak import BleakScanner
from switchbot.const import SwitchbotModel
from switchbot.devices import lock
from switchbot.discovery import GetSwitchbotDevices


def load_config():
    path = Path(__file__).parent / ".switchbot-lock.env"

    for line in path.read_text().splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        name, value = line.split("=", 1)
        os.environ[name] = value.strip().strip("\"'")


async def get_status(mac):
    locks = await GetSwitchbotDevices().get_locks()

    if mac not in locks:
        raise SystemExit("Lock Pro não encontrado por Bluetooth")

    print(locks[mac].data["data"])


async def execute_action(mac, action):
    ble_device = await BleakScanner.find_device_by_address(
        mac,
        timeout=3.0,
    )

    if ble_device is None:
        raise SystemExit("Lock Pro não encontrado por Bluetooth")

    device = lock.SwitchbotLock(
        ble_device,
        os.environ["LOCK_KEY_ID"],
        os.environ["LOCK_ENC_KEY"],
        model=SwitchbotModel.LOCK_PRO,
        scan_timeout=1,
    )

    if action == "lock":
        result = await device.lock()
    elif action == "unlock":
        result = await device.unlock_without_unlatch()
    else:
        result = await device.unlock()

    print(result)


async def main():
    load_config()

    valid_actions = {"status", "lock", "unlock", "open"}

    if len(sys.argv) != 2 or sys.argv[1] not in valid_actions:
        raise SystemExit(
            f"Uso: {sys.argv[0]} status|lock|unlock|open"
        )

    action = sys.argv[1]
    mac = os.environ["LOCK_MAC"].upper()

    if action == "status":
        await get_status(mac)
    else:
        await execute_action(mac, action)


asyncio.run(main())
