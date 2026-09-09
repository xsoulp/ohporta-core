#!/usr/bin/env python3
"""Small HTTP API for the local SwitchBot command scripts."""

import argparse
import hmac
import json
import math
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


BASE_DIR = Path(__file__).resolve().parent

# Access configuration. Door locations can diverge later without changing handlers.
LOCAL_DISTANCE_LIMIT_M = 100.0
DOORS = {
    "APT": {"latitude": 0.42863112, "longitude": -140.21179414},
    "BLD": {"latitude": 0.42863112, "longitude": -140.21179414},
}
ACCESS_POLICIES = {
    "full": {"remote_access": True},
    "restricted": {"remote_access": False},
}
COMMANDS = {
    "/apt_door/status": ("GET", (str(BASE_DIR / "switchbot-lock.py"), "status")),
    "/apt_door/lock": ("POST", (str(BASE_DIR / "switchbot-lock.py"), "lock")),
    "/apt_door/unlock": ("POST", (str(BASE_DIR / "switchbot-lock.py"), "unlock")),
    "/apt_door/open": ("POST", (str(BASE_DIR / "switchbot-lock.py"), "open")),
    "/bld_door": ("POST", (str(BASE_DIR / "switchbot-press.py"),)),
}


def haversine_distance(latitude, longitude, target_latitude, target_longitude):
    lat1 = math.radians(latitude)
    lat2 = math.radians(target_latitude)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(target_longitude - longitude)
    a = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2.0) ** 2
    )
    a = min(1.0, max(0.0, a))
    return 6_371_000.0 * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def valid_coordinates(latitude, longitude):
    return (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90.0 <= latitude <= 90.0
        and -180.0 <= longitude <= 180.0
    )


class SwitchBotHTTPServer(HTTPServer):
    token: str
    restricted_token: str
    command_timeout: float


class Handler(BaseHTTPRequestHandler):
    server_version = "SwitchBotHTTP/1"

    def _reply(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def _access_level(self):
        supplied = self.headers.get("Authorization", "")
        if hmac.compare_digest(supplied, f"Bearer {self.server.token}"):
            return "full"
        if hmac.compare_digest(supplied, f"Bearer {self.server.restricted_token}"):
            return "restricted"
        return None

    def _restricted_distance(self, door_name):
        try:
            latitude = float(self.headers["X-Latitude"])
            longitude = float(self.headers["X-Longitude"])
        except (KeyError, TypeError, ValueError):
            return None

        if not valid_coordinates(latitude, longitude):
            return None

        door = DOORS[door_name]
        return haversine_distance(
            latitude,
            longitude,
            door["latitude"],
            door["longitude"],
        )

    def _query_coordinates(self):
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        latitude_values = query.get("lat", [])
        longitude_values = query.get("lon", [])
        if len(latitude_values) != 1 or len(longitude_values) != 1:
            return None
        try:
            latitude = float(latitude_values[0])
            longitude = float(longitude_values[0])
        except (TypeError, ValueError):
            return None
        if not valid_coordinates(latitude, longitude):
            return None
        return latitude, longitude

    def _capabilities(self, access_level, latitude, longitude):
        remote_access = ACCESS_POLICIES[access_level]["remote_access"]
        doors = {}
        distances = []
        for door_name, door in DOORS.items():
            distance = haversine_distance(
                latitude,
                longitude,
                door["latitude"],
                door["longitude"],
            )
            distances.append(distance)
            is_remote = distance >= LOCAL_DISTANCE_LIMIT_M
            allowed = not is_remote or remote_access
            doors[door_name] = {
                "distance_m": round(distance, 1),
                "can_open": allowed,
                "mode": "remote" if is_remote else "local",
                "requires_warning": is_remote and allowed,
                "reason": None if allowed else "remote_access_not_permitted",
            }

        nearest_distance = min(distances)
        return {
            "location": {
                "lat": latitude,
                "lon": longitude,
                "distance_m": round(nearest_distance, 1),
                "remote": nearest_distance >= LOCAL_DISTANCE_LIMIT_M,
            },
            "local_distance_limit_m": LOCAL_DISTANCE_LIMIT_M,
            "doors": doors,
        }

    def _unauthorized(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _method_not_allowed(self, allowed_method):
        self.send_response(405)
        self.send_header("Allow", allowed_method)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path == "/health" and self.command == "GET":
            self._reply(200, {"ok": True})
            return

        if path == "/access/capabilities":
            if self.command != "GET":
                self._method_not_allowed("GET")
                return
            access_level = self._access_level()
            if access_level is None:
                self._unauthorized()
                return
            coordinates = self._query_coordinates()
            if coordinates is None:
                self._reply(400, {"ok": False, "error": "invalid coordinates"})
                return
            self._reply(200, self._capabilities(access_level, *coordinates))
            return

        spec = COMMANDS.get(path)
        if spec is None:
            self._reply(404, {"ok": False, "error": "endpoint not found"})
            return

        method, argv = spec
        if self.command != method:
            self._method_not_allowed(method)
            return

        access_level = self._access_level()
        if access_level is None:
            self._unauthorized()
            return

        if access_level == "restricted":
            door_name = "APT" if path.startswith("/apt_door/") else "BLD"
            distance = self._restricted_distance(door_name)
            if distance is None:
                self._reply(
                    400,
                    {"ok": False, "error": "coordenadas GPS obrigatórias ou inválidas"},
                )
                return
            if distance >= LOCAL_DISTANCE_LIMIT_M:
                self._reply(
                    403,
                    {
                        "ok": False,
                        "error": "muito longe de casa",
                        "distance_m": round(distance, 1),
                    },
                )
                return

        try:
            result = subprocess.run(
                argv,
                cwd=BASE_DIR,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.server.command_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self._reply(504, {"ok": False, "error": "command timed out"})
            return
        except OSError as exc:
            self._reply(500, {"ok": False, "error": str(exc)})
            return

        output = result.stdout.strip()
        error = result.stderr.strip()
        if result.returncode == 0:
            self._reply(200, {"ok": True, "output": output})
        else:
            details = error or output
            device = "apt_door" if path.startswith("/apt_door/") else "bld_door"
            config_error = any(
                marker in details
                for marker in (
                    ".switchbot-lock.env",
                    ".switchbot-bot.env",
                    "PermissionError",
                    "FileNotFoundError",
                )
            )
            if config_error:
                self._reply(
                    503,
                    {"ok": False, "error": f"{device} configuration unavailable"},
                )
            elif "Traceback (most recent call last)" in details:
                self._reply(502, {"ok": False, "error": f"{device} command failed"})
            else:
                self._reply(502, {"ok": False, "error": details or "command failed"})

    do_GET = _handle
    do_POST = _handle

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}", flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--restricted-token-file", type=Path, required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    token = args.token_file.expanduser().read_text().strip()
    restricted_token = args.restricted_token_file.expanduser().read_text().strip()
    if not token:
        raise SystemExit("token file is empty")
    if not restricted_token:
        raise SystemExit("restricted token file is empty")
    if hmac.compare_digest(token, restricted_token):
        raise SystemExit("tokens must be different")

    server = SwitchBotHTTPServer((args.host, args.port), Handler)
    server.token = token
    server.restricted_token = restricted_token
    server.command_timeout = args.timeout
    print(f"Listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
