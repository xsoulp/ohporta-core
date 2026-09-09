#!/home/xsoulp/switchbot-venv/bin/python

import asyncio
import os
from pathlib import Path

from bleak import BleakScanner
from switchbot.devices.bot import Switchbot


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

    ble_device = await BleakScanner.find_device_by_address(
        mac,
        timeout=5.0,
    )

    if ble_device is None:
        raise SystemExit("SwitchBot Bot não encontrado por Bluetooth")

    device = Switchbot(
        ble_device,
        password=password,
        retry_count=3,
    )

    result = await device.press()
    print(result)


asyncio.run(main())
