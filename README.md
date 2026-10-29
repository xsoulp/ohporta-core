# Home automation server

A local HTTP API for controlling devices via Bluetooth, particularly SwitchBot
devices. It currently supports a SwitchBot Lock Pro smart lock and a SwitchBot
Bot. This is independent of the Android application maintained at https://github.com/xsoulp/ohporta-android

## Installation

Requires Python 3.13, BlueZ, and a working Bluetooth adapter.

```sh
cd ~/ohporta-core
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .server.env.example .server.env
cp .switchbot-bot.env.example .switchbot-bot.env
cp .switchbot-lock.env.example .switchbot-lock.env
```

All commands below assume you are in the repository directory with `.venv`
activated. In a new shell, run `source .venv/bin/activate` first.

Fill in the configuration below. For an existing installation, preserve
`users.json`: it contains the accounts and bearer token hashes. The `.env`
files, tokens, `users.json`, and access history contain private data and must
not be committed to Git.

The `users.example.json` file documents only the empty structure and must not be
used to initialize authentication.

## Configuration files

Place these files beside `ohporta-server.py`. Use `NAME=value` lines, without
an `export` prefix. Replace the example addresses, keys and coordinates with
your installation's values.

| File | Variable | What to fill in |
| --- | --- | --- |
| `.server.env` | `APT_LATITUDE`, `APT_LONGITUDE` | Apartment door coordinates in decimal degrees. |
| `.server.env` | `BLD_LATITUDE`, `BLD_LONGITUDE` | Building entrance coordinates in decimal degrees. |
| `.server.env` | `LOCAL_DISTANCE_LIMIT_M` | Distance in metres within which a request counts as local; default `100.0`. |
| `.switchbot-bot.env` | `BOT_MAC` | Bluetooth MAC address of the building entrance Bot, such as `AA:BB:CC:DD:EE:FF`. |
| `.switchbot-bot.env` | `BOT_PASSWORD` | The Bot's device password; use `BOT_PASSWORD=` if no device password is set. This is separate from your SwitchBot account password. |
| `.switchbot-bot.env` | `BOT_SCAN_ATTEMPTS` | Optional discovery attempts; default `3`. |
| `.switchbot-bot.env` | `BOT_SCAN_TIMEOUT` | Optional timeout in seconds per discovery attempt; default `10`. |
| `.switchbot-lock.env` | `LOCK_MAC` | Bluetooth MAC address of the apartment Lock Pro. |
| `.switchbot-lock.env` | `LOCK_KEY_ID` | The lock's encryption key ID: two hexadecimal characters. |
| `.switchbot-lock.env` | `LOCK_ENC_KEY` | The lock's encryption key: 32 hexadecimal characters. |

Use the key ID and encryption key obtained for your specific lock. Neither is
the HTTP bearer token. Coordinates set to `0.0` in the example are placeholders.

## First run

Fill in the `.env` files above, then create an admin token:

```sh
admin_token_file=$(mktemp)
python -c 'import secrets; print(secrets.token_urlsafe(32))' > "$admin_token_file"
cat "$admin_token_file"
```

Save the displayed token in your password manager. Start the server:

```sh
python ohporta-server.py --token-file "$admin_token_file"
```

This reads `.server.env` and creates `users.json` with the admin account.
The admin token comes from the temporary file; it does not go in `.env`.
The administrator authenticates with `Authorization: Bearer <your-admin-token>`.
Add other users through
`POST /admin/users`; see the [API documentation](readme_api.md#user-administration).

After startup succeeds, stop with Ctrl+C and delete the temporary token file:

```sh
rm -- "$admin_token_file"
unset admin_token_file
```

## Stop and restart

Stop the server with Ctrl+C. To start it again:

```sh
python ohporta-server.py
```

Every start reads `.server.env` and the existing accounts from `users.json`.
No token file is needed again. Keep `users.json` to preserve users and tokens.
The device scripts read their respective `.env` files whenever they run.
The persistent Bluetooth backend also rereads the device configuration before
each operation and replaces cached devices when their configuration changes.

For migration only, `--restricted-token-file` can optionally import an existing
regular-user token alongside the admin token during first-time setup.

## Service

The service uses `.venv/bin/python` directly because systemd does not inherit
your shell's activation. The template assumes the checkout is at
`~/ohporta-core`; adjust `WorkingDirectory`, `ExecStart`, and `Environment`
in the service file if your checkout is elsewhere.

```sh
mkdir -p ~/.config/systemd/user
cp switchbot-http.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now switchbot-http.service
```

Stop, restart, status and logs:

```sh
systemctl --user stop switchbot-http.service
systemctl --user restart switchbot-http.service
systemctl --user status switchbot-http.service
journalctl --user -u switchbot-http.service -f
```

Main endpoints:

```text
GET  /health
GET  /apt_door/status
POST /apt_door/lock
POST /apt_door/unlock
POST /apt_door/open
POST /bld_door
```

See [readme_api.md](readme_api.md) for the complete API documentation.

## Raspberry Pi performance

The server defaults to `--bluetooth-mode persistent`. A dedicated Bluetooth
event loop imports the device library once, reuses discovered devices, and
reuses GATT connections between nearby requests. The library still disconnects
after about 8.5 seconds of inactivity; no continuous scan or battery-draining
keepalive is used. The configured MAC is first looked up in BlueZ's known
devices, allowing commands to connect without a discovery scan even on the
first request after service startup. Discovery is a fallback only when BlueZ
has no matching device, for example after it forgets an unpaired device on an
adapter restart. Later operations reuse the same device object.

Lock status requests wait for the first valid, fresh advertisement instead of
waiting for a full five-second scan. They never serve cached door status and
may need to release an existing lock connection before scanning. Physical
operations still require device acknowledgement, and the Bot retains its
warm-up for new or reopened connections. Existing library retries remain in
place; the backend does not replay failed operations or automatically switch
modes after an error.

HTTP requests run concurrently, so health, permissions, and administration
remain responsive during Bluetooth operations. Only one Bluetooth operation
runs at a time. An overlapping device request receives `409 device_busy`
instead of being queued. A command timeout can require up to three additional
seconds to clean up the connection before returning `504`.

After updating the checkout on the Pi, restart the service:

```sh
systemctl --user restart switchbot-http.service
journalctl --user -u switchbot-http.service -f
```

Each device request logs its elapsed time as `device command /... completed in
...s`, including discovery, command execution, and history persistence. Compare
the same operation on the Pi, both after startup and on repeated requests.
The actual improvement depends on radio conditions, BlueZ, and the device;
hardware latency must be measured on the Pi.

To compare with the original subprocess backend, stop the service and run:

```sh
systemctl --user stop switchbot-http.service
python ohporta-server.py --bluetooth-mode subprocess
```

Stop the comparison process with Ctrl+C and restart the service to restore the
persistent backend. The standalone device scripts remain available.

Hardware-independent regression checks:

```sh
python -m unittest discover -s tests -v
```

## Bundled SwitchBot code

The complete PySwitchbot 2.7.0 Python source is included in
[`switchbot/`](switchbot/). The scripts import this local copy directly;
installing PySwitchbot or cloning its upstream repository is unnecessary.
Generic Bluetooth, HTTP and cryptography dependencies are still installed
through `requirements.txt`.

The source, version and MIT license are documented in
[`third_party/pyswitchbot/README.md`](third_party/pyswitchbot/README.md).
We can maintain and modify this copy here; upstream updates are optional and
manual. For an existing installation, rerun `pip install -r requirements.txt`
in its virtual environment and restart the service after updating the checkout.
