#!/usr/bin/env python3
"""Small HTTP API for the local SwitchBot command scripts."""

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import subprocess
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).resolve().parent
LISBON = ZoneInfo("Europe/Lisbon")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
LEGACY_SCHEDULE_FIELDS = {"allowed_days", "start_time", "end_time"}
USER_FIELDS = {"name", "enabled", "allow_remote", "schedules"} | LEGACY_SCHEDULE_FIELDS
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
MAX_HISTORY_ENTRIES = 5_000


def load_env_file(path):
    """Load a simple KEY=VALUE file without overriding service environment values."""
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name, value.strip().strip("\"'"))


def required_float(name):
    value = os.environ.get(name)
    if value is None:
        raise RuntimeError(
            f"{name} is required; copy .server.env.example to .server.env"
        )
    try:
        return float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc


load_env_file(BASE_DIR / ".server.env")

LOCAL_DISTANCE_LIMIT_M = float(os.environ.get("LOCAL_DISTANCE_LIMIT_M", "100.0"))
DOORS = {
    "APT": {
        "latitude": required_float("APT_LATITUDE"),
        "longitude": required_float("APT_LONGITUDE"),
    },
    "BLD": {
        "latitude": required_float("BLD_LATITUDE"),
        "longitude": required_float("BLD_LONGITUDE"),
    },
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
    a = math.sin(delta_lat / 2.0) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2.0) ** 2
    a = min(1.0, max(0.0, a))
    return 6_371_000.0 * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def valid_coordinates(latitude, longitude):
    return math.isfinite(latitude) and math.isfinite(longitude) and -90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0


def current_lisbon_time():
    return datetime.now(LISBON)


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def public_user(user):
    return {key: value for key, value in user.items() if key != "token_hash"}


def _validate_schedule(schedule):
    if not isinstance(schedule, dict):
        raise ValueError("each schedule must be an object")
    required = LEGACY_SCHEDULE_FIELDS
    if set(schedule) != required:
        raise ValueError("each schedule requires only allowed_days, start_time and end_time")
    days = schedule["allowed_days"]
    if not isinstance(days, list) or any(day not in WEEKDAYS for day in days):
        raise ValueError(f"allowed_days must contain only: {', '.join(WEEKDAYS)}")
    if len(days) != len(set(days)):
        raise ValueError("allowed_days must not contain duplicates")
    result = {"allowed_days": [day for day in WEEKDAYS if day in days]}
    for field in ("start_time", "end_time"):
        value = schedule[field]
        if not isinstance(value, str) or not TIME_PATTERN.fullmatch(value):
            raise ValueError(f"{field} must use HH:MM (24-hour) format")
        result[field] = value
    return result


def _validate_user_fields(data, *, require_name=False):
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    unknown = set(data) - USER_FIELDS
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
    if require_name and "name" not in data:
        raise ValueError("name is required")
    if "schedules" in data and LEGACY_SCHEDULE_FIELDS.intersection(data):
        raise ValueError("schedules cannot be combined with legacy schedule fields")
    cleaned = {}
    if "name" in data:
        name = data["name"]
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise ValueError("name must contain 1 to 100 characters")
        cleaned["name"] = name.strip()
    for field in ("enabled", "allow_remote"):
        if field in data:
            if not isinstance(data[field], bool):
                raise ValueError(f"{field} must be a boolean")
            cleaned[field] = data[field]
    if "schedules" in data:
        schedules = data["schedules"]
        if not isinstance(schedules, list) or len(schedules) > 32:
            raise ValueError("schedules must be a list with at most 32 periods")
        cleaned["schedules"] = [_validate_schedule(schedule) for schedule in schedules]
    legacy = {field: data[field] for field in LEGACY_SCHEDULE_FIELDS if field in data}
    if legacy:
        defaults = {"allowed_days": list(WEEKDAYS), "start_time": "00:00", "end_time": "23:59"}
        defaults.update(legacy)
        validated = _validate_schedule(defaults)
        for field in LEGACY_SCHEDULE_FIELDS:
            if field in legacy:
                cleaned[field] = validated[field]
    return cleaned


def _normalise_user_schedule(user):
    if "schedules" not in user:
        user["schedules"] = [{
            "allowed_days": user.pop("allowed_days", list(WEEKDAYS)),
            "start_time": user.pop("start_time", "00:00"),
            "end_time": user.pop("end_time", "23:59"),
        }]
    return user


class UserStore:
    """Small atomic JSON store. Only token hashes are persisted."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def exists(self):
        return self.path.exists()

    def _load(self):
        if not self.path.exists():
            return {"next_id": 1, "users": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("users"), list):
            raise ValueError("invalid users database")
        for user in data["users"]:
            _normalise_user_schedule(user)
        return data

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def bootstrap_users(self, admin_token, restricted_token=None):
        """Create the admin, optionally importing a legacy regular-user token."""
        with self._lock:
            if self.path.exists():
                return False
            if not admin_token:
                raise ValueError("an admin token is required for first-time setup; use --token-file")
            if restricted_token is not None:
                if not restricted_token:
                    raise ValueError("the optional restricted token must not be empty")
                if hmac.compare_digest(admin_token, restricted_token):
                    raise ValueError("admin and restricted tokens must be different")
            data = {
                "next_id": 2,
                "users": [
                    {"id": 1, "name": "Administrator", "token_hash": token_hash(admin_token), "role": "admin", "enabled": True, "allow_remote": True, "schedules": [{"allowed_days": list(WEEKDAYS), "start_time": "00:00", "end_time": "23:59"}]},
                ],
            }
            if restricted_token is not None:
                data["users"].append(
                    {"id": 2, "name": "Restricted user", "token_hash": token_hash(restricted_token), "role": "user", "enabled": True, "allow_remote": True, "schedules": [{"allowed_days": list(WEEKDAYS), "start_time": "00:00", "end_time": "23:59"}]}
                )
                data["next_id"] = 3
            self._save(data)
            return True

    def authenticate(self, token):
        supplied_hash = token_hash(token)
        with self._lock:
            for user in self._load()["users"]:
                if hmac.compare_digest(supplied_hash, user["token_hash"]):
                    return dict(user)
        return None

    def list_users(self):
        with self._lock:
            return [public_user(user) for user in self._load()["users"]]

    def get_user(self, user_id):
        with self._lock:
            for user in self._load()["users"]:
                if user["id"] == user_id:
                    return public_user(user)
        return None

    def create_user(self, values):
        cleaned = _validate_user_fields(values, require_name=True)
        schedules = cleaned.pop("schedules", None)
        if schedules is None:
            schedules = [{
                "allowed_days": cleaned.pop("allowed_days", list(WEEKDAYS)),
                "start_time": cleaned.pop("start_time", "00:00"),
                "end_time": cleaned.pop("end_time", "23:59"),
            }]
        token = secrets.token_urlsafe(32)
        with self._lock:
            data = self._load()
            user = {
                "id": data["next_id"], "name": cleaned["name"], "token_hash": token_hash(token), "role": "user",
                "enabled": cleaned.get("enabled", True), "allow_remote": cleaned.get("allow_remote", False),
                "schedules": schedules,
            }
            data["next_id"] += 1
            data["users"].append(user)
            self._save(data)
        result = public_user(user)
        result["token"] = token
        return result

    def update_user(self, user_id, values):
        cleaned = _validate_user_fields(values)
        if not cleaned:
            raise ValueError("at least one editable field is required")
        with self._lock:
            data = self._load()
            for user in data["users"]:
                if user["id"] == user_id:
                    legacy = {field: cleaned.pop(field) for field in tuple(LEGACY_SCHEDULE_FIELDS) if field in cleaned}
                    if legacy:
                        if len(user["schedules"]) != 1:
                            raise ValueError("use schedules when the user has multiple periods")
                        schedule = dict(user["schedules"][0])
                        schedule.update(legacy)
                        cleaned["schedules"] = [_validate_schedule(schedule)]
                    user.update(cleaned)
                    self._save(data)
                    return public_user(user)
        return None

    def delete_user(self, user_id):
        with self._lock:
            data = self._load()
            for index, user in enumerate(data["users"]):
                if user["id"] == user_id:
                    deleted = data["users"].pop(index)
                    self._save(data)
                    return public_user(deleted)
        return None


class AccessHistoryStore:
    """Atomic append-only audit history with bounded on-disk retention."""

    def __init__(self, path, max_entries=MAX_HISTORY_ENTRIES):
        self.path = Path(path)
        self.max_entries = max_entries
        self._lock = threading.RLock()

    def _load(self):
        if not self.path.exists():
            return {"next_id": 1, "entries": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("next_id"), int)
            or not isinstance(data.get("entries"), list)
        ):
            raise ValueError("invalid access history database")
        return data

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def record_action(self, user, door, action):
        with self._lock:
            data = self._load()
            entry = {
                "id": data["next_id"],
                "user_id": user["id"],
                "user_name": user["name"],
                "door": door,
                "action": action,
                "opened_at": current_lisbon_time().isoformat(timespec="seconds"),
            }
            data["next_id"] += 1
            data["entries"].append(entry)
            if len(data["entries"]) > self.max_entries:
                data["entries"] = data["entries"][-self.max_entries:]
            self._save(data)
            return dict(entry)

    def list_entries(self, limit):
        with self._lock:
            entries = self._load()["entries"]
            result = []
            for entry in reversed(entries[-limit:]):
                item = dict(entry)
                item.setdefault("action", "open")
                result.append(item)
            return result


def _schedule_period_allows(schedule, now):
    current_time = now.strftime("%H:%M")
    start, end = schedule["start_time"], schedule["end_time"]
    today = WEEKDAYS[now.weekday()]
    if start <= end:
        return today in schedule["allowed_days"] and start <= current_time <= end
    if current_time >= start:
        return today in schedule["allowed_days"]
    if current_time <= end:
        previous_day = WEEKDAYS[(now - timedelta(days=1)).weekday()]
        return previous_day in schedule["allowed_days"]
    return False


def schedule_allows(user, now):
    return any(_schedule_period_allows(schedule, now) for schedule in user["schedules"])


def access_decision(user, is_remote, now=None):
    if not user["enabled"]:
        return False, "user_disabled"
    if user["role"] == "admin":
        return True, None
    if not schedule_allows(user, now or current_lisbon_time()):
        return False, "outside_allowed_schedule"
    if is_remote and not user["allow_remote"]:
        return False, "remote_access_not_allowed"
    return True, None


class SwitchBotHTTPServer(HTTPServer):
    command_timeout: float
    user_store: UserStore
    history_store: AccessHistoryStore


class Handler(BaseHTTPRequestHandler):
    server_version = "SwitchBotHTTP/2"

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

    def _empty_reply(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0 or length > 65_536:
            raise ValueError("JSON body is required and must not exceed 65536 bytes")
        try:
            return json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON body") from exc

    def _authenticated_user(self):
        supplied = self.headers.get("Authorization", "")
        scheme, separator, token = supplied.partition(" ")
        if separator != " " or scheme != "Bearer" or not token or " " in token:
            return None
        return self.server.user_store.authenticate(token)

    def _require_user(self):
        user = self._authenticated_user()
        if user is None:
            self._unauthorized()
        return user

    def _require_admin(self):
        user = self._require_user()
        if user is None:
            return None
        if not user["enabled"] or user["role"] != "admin":
            self._reply(403, {"ok": False, "error": "admin_required"})
            return None
        return user

    def _header_coordinates(self):
        try:
            latitude = float(self.headers["X-Latitude"])
            longitude = float(self.headers["X-Longitude"])
        except (KeyError, TypeError, ValueError):
            return None
        return (latitude, longitude) if valid_coordinates(latitude, longitude) else None

    def _query_coordinates(self):
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        latitude_values, longitude_values = query.get("lat", []), query.get("lon", [])
        if len(latitude_values) != 1 or len(longitude_values) != 1:
            return None
        try:
            latitude, longitude = float(latitude_values[0]), float(longitude_values[0])
        except (TypeError, ValueError):
            return None
        return (latitude, longitude) if valid_coordinates(latitude, longitude) else None

    def _door_distance(self, coordinates, door_name):
        door = DOORS[door_name]
        return haversine_distance(coordinates[0], coordinates[1], door["latitude"], door["longitude"])

    def _capabilities(self, user, latitude, longitude):
        doors, distances = {}, []
        now = current_lisbon_time()
        for door_name in DOORS:
            distance = self._door_distance((latitude, longitude), door_name)
            distances.append(distance)
            is_remote = distance >= LOCAL_DISTANCE_LIMIT_M
            allowed, reason = access_decision(user, is_remote, now)
            doors[door_name] = {"distance_m": round(distance, 1), "can_open": allowed, "mode": "remote" if is_remote else "local", "requires_warning": is_remote and allowed, "reason": reason}
        nearest_distance = min(distances)
        return {
            "user": {key: user[key] for key in ("id", "name", "role")},
            "location": {"lat": latitude, "lon": longitude, "distance_m": round(nearest_distance, 1), "remote": nearest_distance >= LOCAL_DISTANCE_LIMIT_M},
            "local_distance_limit_m": LOCAL_DISTANCE_LIMIT_M,
            "doors": doors,
        }

    def _unauthorized(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _method_not_allowed(self, allowed_methods):
        self.send_response(405)
        self.send_header("Allow", ", ".join(allowed_methods))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_admin(self, path):
        if self._require_admin() is None:
            return
        if path == "/admin/access-history":
            if self.command != "GET":
                self._method_not_allowed(("GET",))
                return
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            try:
                limit_values = query.get("limit", ["50"])
                if len(limit_values) != 1:
                    raise ValueError
                limit = int(limit_values[0])
                if not 1 <= limit <= 200:
                    raise ValueError
            except (TypeError, ValueError):
                self._reply(400, {"ok": False, "error": "limit must be between 1 and 200"})
                return
            self._reply(200, {"entries": self.server.history_store.list_entries(limit)})
            return
        if path == "/admin/users":
            if self.command == "GET":
                self._reply(200, {"users": self.server.user_store.list_users()})
            elif self.command == "POST":
                try:
                    self._reply(201, self.server.user_store.create_user(self._json_body()))
                except ValueError as exc:
                    self._reply(400, {"ok": False, "error": str(exc)})
            else:
                self._method_not_allowed(("GET", "POST"))
            return
        match = re.fullmatch(r"/admin/users/(\d+)", path)
        if match is None:
            self._reply(404, {"ok": False, "error": "endpoint not found"})
            return
        user_id = int(match.group(1))
        if self.command == "GET":
            user = self.server.user_store.get_user(user_id)
            self._reply(200, user) if user is not None else self._reply(404, {"ok": False, "error": "user_not_found"})
        elif self.command == "PUT":
            try:
                user = self.server.user_store.update_user(user_id, self._json_body())
            except ValueError as exc:
                self._reply(400, {"ok": False, "error": str(exc)})
                return
            self._reply(200, user) if user is not None else self._reply(404, {"ok": False, "error": "user_not_found"})
        elif self.command == "DELETE":
            if self.server.user_store.delete_user(user_id) is None:
                self._reply(404, {"ok": False, "error": "user_not_found"})
            else:
                self._empty_reply(204)
        else:
            self._method_not_allowed(("GET", "PUT", "DELETE"))

    def _execute_command(self, path, argv, user):
        try:
            result = subprocess.run(argv, cwd=BASE_DIR, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=self.server.command_timeout, check=False)
        except subprocess.TimeoutExpired:
            self._reply(504, {"ok": False, "error": "command timed out"})
            return
        except OSError as exc:
            self._reply(500, {"ok": False, "error": str(exc)})
            return
        output, error = result.stdout.strip(), result.stderr.strip()
        if result.returncode == 0:
            if path in ("/apt_door/open", "/apt_door/lock", "/apt_door/unlock", "/bld_door"):
                door = "APT" if path.startswith("/apt_door/") else "BLD"
                action = path.rsplit("/", 1)[-1] if door == "APT" else "open"
                try:
                    self.server.history_store.record_action(user, door, action)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    print(f"Could not persist access history: {exc}", flush=True)
            self._reply(200, {"ok": True, "output": output})
            return
        details = error or output
        device = "apt_door" if path.startswith("/apt_door/") else "bld_door"
        config_error = any(marker in details for marker in (".switchbot-lock.env", ".switchbot-bot.env", "PermissionError", "FileNotFoundError"))
        if config_error:
            self._reply(503, {"ok": False, "error": f"{device} configuration unavailable"})
        elif "Traceback (most recent call last)" in details:
            self._reply(502, {"ok": False, "error": f"{device} command failed"})
        else:
            self._reply(502, {"ok": False, "error": details or "command failed"})

    def _handle(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health" and self.command == "GET":
            self._reply(200, {"ok": True})
            return
        if path == "/access/capabilities":
            if self.command != "GET":
                self._method_not_allowed(("GET",))
                return
            user = self._require_user()
            if user is None:
                return
            coordinates = self._query_coordinates()
            if coordinates is None:
                self._reply(400, {"ok": False, "error": "invalid coordinates"})
                return
            self._reply(200, self._capabilities(user, *coordinates))
            return
        if path == "/admin/access-history" or path == "/admin/users" or path.startswith("/admin/users/"):
            self._handle_admin(path)
            return
        spec = COMMANDS.get(path)
        if spec is None:
            self._reply(404, {"ok": False, "error": "endpoint not found"})
            return
        method, argv = spec
        if self.command != method:
            self._method_not_allowed((method,))
            return
        user = self._require_user()
        if user is None:
            return
        if not user["enabled"]:
            self._reply(403, {"ok": False, "error": "user_disabled", "reason": "user_disabled"})
            return
        if user["role"] != "admin":
            coordinates = self._header_coordinates()
            if coordinates is None:
                self._reply(400, {"ok": False, "error": "coordenadas GPS obrigatórias ou inválidas"})
                return
            door_name = "APT" if path.startswith("/apt_door/") else "BLD"
            distance = self._door_distance(coordinates, door_name)
            allowed, reason = access_decision(user, distance >= LOCAL_DISTANCE_LIMIT_M)
            if not allowed:
                self._reply(403, {"ok": False, "error": reason, "reason": reason, "distance_m": round(distance, 1)})
                return
        self._execute_command(path, argv, user)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}", flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--users-file", type=Path, default=BASE_DIR / "users.json")
    parser.add_argument("--history-file", type=Path, default=BASE_DIR / "access-history.json")
    parser.add_argument("--token-file", type=Path, help="admin token file, required for first-time setup")
    parser.add_argument("--restricted-token-file", type=Path, help="optional legacy regular-user token file for migration")
    return parser.parse_args()


def _read_optional_token(path):
    return path.expanduser().read_text().strip() if path else None


def main():
    args = parse_args()
    user_store = UserStore(args.users_file.expanduser())
    if not user_store.exists():
        try:
            user_store.bootstrap_users(_read_optional_token(args.token_file), _read_optional_token(args.restricted_token_file))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"could not initialize users: {exc}") from exc
        print(f"Initialized users in {user_store.path}", flush=True)
    else:
        for option, path in (
            ("--token-file", args.token_file),
            ("--restricted-token-file", args.restricted_token_file),
        ):
            if path is not None:
                print(
                    f"Ignored {option} file {path}: users database {user_store.path} already exists.",
                    flush=True,
                )
    server = SwitchBotHTTPServer((args.host, args.port), Handler)
    server.user_store = user_store
    server.history_store = AccessHistoryStore(args.history_file.expanduser())
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
