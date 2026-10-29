# Home automation server API

Base URL used in the examples:

```text
https://example.invalid
```

Replace all values enclosed in `<...>` with actual values. All endpoints,
except `GET /health`, require:

```http
Authorization: Bearer <TOKEN>
```

Operation endpoints receive coordinates in the headers:

```http
X-Latitude: <LATITUDE>
X-Longitude: <LONGITUDE>
```

Allowed days use `monday`, `tuesday`, `wednesday`, `thursday`,
`friday`, `saturday`, and `sunday`. Times use `HH:MM` and are evaluated in the
`Europe/Lisbon` time zone.

## Server status

### `GET /health`

Checks whether the HTTP server is running. No authentication required.

Payload: no body.

Response `200 OK`:

```json
{
  "ok": true
}
```

## User permissions

### `GET /access/capabilities?lat=<LATITUDE>&lon=<LONGITUDE>`

Identifies the user by their bearer token and determines whether they can
operate each door, taking into account their status, role, schedule, day,
distance, and remote access permissions.

Payload: no body. Coordinates are sent in the query string.

Response `200 OK`:

```json
{
  "user": {
    "id": <USER_ID>,
    "name": "<NAME>",
    "role": "<admin|user>"
  },
  "location": {
    "lat": <LATITUDE>,
    "lon": <LONGITUDE>,
    "distance_m": <DISTANCE_METERS>,
    "remote": <true|false>
  },
  "local_distance_limit_m": 100.0,
  "doors": {
    "APT": {
      "distance_m": <DISTANCE_METERS>,
      "can_open": <true|false>,
      "mode": "<local|remote>",
      "requires_warning": <true|false>,
      "reason": <null|"user_disabled"|"outside_allowed_schedule"|"remote_access_not_allowed">
    },
    "BLD": {
      "distance_m": <DISTANCE_METERS>,
      "can_open": <true|false>,
      "mode": "<local|remote>",
      "requires_warning": <true|false>,
      "reason": <null|"user_disabled"|"outside_allowed_schedule"|"remote_access_not_allowed">
    }
  }
}
```

Response `400 Bad Request`, missing or invalid coordinates:

```json
{
  "ok": false,
  "error": "invalid coordinates"
}
```

## Apartment door

The following endpoints repeat all backend checks before calling the device.
Enabled `admin` users can operate without GPS headers.

### `GET /apt_door/status`

Retrieves the status of the apartment door lock.

Payload: no body.

Response `200 OK`:

```json
{
  "ok": true,
  "output": "<STATUS_RETURNED_BY_SWITCHBOT>"
}
```

### `POST /apt_door/open`

Unlocks and opens the apartment door.

Payload: no body; send the bearer token and coordinates in the headers.

Response `200 OK`:

```json
{
  "ok": true,
  "output": "<SWITCHBOT_RESULT>"
}
```

### `POST /apt_door/unlock`

Unlocks the apartment door without opening it.

Payload: no body; send the bearer token and coordinates in the headers.

Response `200 OK`:

```json
{
  "ok": true,
  "output": "<SWITCHBOT_RESULT>"
}
```

### `POST /apt_door/lock`

Locks the apartment door.

Payload: no body; send the bearer token and coordinates in the headers.

Response `200 OK`:

```json
{
  "ok": true,
  "output": "<SWITCHBOT_RESULT>"
}
```

## Building door

### `POST /bld_door`

Triggers the SwitchBot Bot to press the button that opens the building door.

Payload: no body; send the bearer token and coordinates in the headers.

Response `200 OK`:

```json
{
  "ok": true,
  "output": "<SWITCHBOT_RESULT>"
}
```

## User administration

All endpoints in this section require the bearer token of an enabled `admin`
user. Responses never include the token or its hash, except for the full token
returned once by `POST /admin/users`.
The administrator authenticates using only their own bearer token; they do not
need other users' tokens.

Standard user representation:

```json
{
  "id": <USER_ID>,
  "name": "<NAME>",
  "role": "<admin|user>",
  "enabled": <true|false>,
  "allow_remote": <true|false>,
  "schedules": [
    {
      "allowed_days": ["<DAY_1>", "<DAY_2>"],
      "start_time": "<HH:MM>",
      "end_time": "<HH:MM>"
    }
  ]
}
```

`allow_remote: false` permits only local operations, within 100 meters.
`allow_remote: true` permits operations at any distance. The 100-meter local
limit is fixed in the backend.

Each item in `schedules` is an independent time period. The user can operate
when at least one period matches the current day and time. An empty list
does not allow operations at any time. For compatibility, the legacy fields
`allowed_days`, `start_time`, and `end_time` can still be sent without
`schedules`; the server converts them into a single period.

### `GET /admin/access-history?limit=<1..200>`

Lists the most recent door operations, from newest to oldest.
The `limit` parameter is optional and defaults to `50`. Only successful actions
on `POST /apt_door/open`, `POST /apt_door/lock`,
`POST /apt_door/unlock`, and `POST /bld_door` are recorded.

Response `200 OK`:

```json
{
  "entries": [
    {
      "id": 42,
      "user_id": 2,
      "user_name": "Example user",
      "door": "APT",
      "action": "unlock",
      "opened_at": "2026-09-10T20:15:30+01:00"
    }
  ]
}
```

The history is saved atomically to `access-history.json` with `0600`
permissions, retaining the 5,000 most recent entries.

### `GET /admin/users`

Lists all users.

Payload: no body.

Response `200 OK`:

```json
{
  "users": [
    {
      "id": <USER_ID>,
      "name": "<NAME>",
      "role": "<admin|user>",
      "enabled": <true|false>,
      "allow_remote": <true|false>,
      "schedules": [
        {
          "allowed_days": ["<DAY_1>", "<DAY_2>"],
          "start_time": "<HH:MM>",
          "end_time": "<HH:MM>"
        }
      ]
    }
  ]
}
```

### `POST /admin/users`

Creates a user with `role=user` and generates a secure token. `name` is
required; the remaining fields are optional.

Payload:

```json
{
  "name": "<NAME>",
  "enabled": <true|false>,
  "allow_remote": <true|false>,
  "schedules": [
    {
      "allowed_days": ["monday", "tuesday", "wednesday"],
      "start_time": "08:00",
      "end_time": "12:00"
    },
    {
      "allowed_days": ["monday", "tuesday", "wednesday"],
      "start_time": "14:00",
      "end_time": "20:00"
    }
  ]
}
```

Response `201 Created`:

```json
{
  "id": <USER_ID>,
  "name": "<NAME>",
  "role": "user",
  "enabled": <true|false>,
  "allow_remote": <true|false>,
  "schedules": [
    {
      "allowed_days": ["<DAY_1>", "<DAY_2>"],
      "start_time": "<HH:MM>",
      "end_time": "<HH:MM>"
    }
  ],
  "token": "<FULL_TOKEN_SAVE_NOW>"
}
```

### `GET /admin/users/{id}`

Retrieves a user by ID.

Example endpoint:

```text
GET /admin/users/<USER_ID>
```

Payload: no body.

Response `200 OK`: standard user representation, without a token.

Response `404 Not Found`:

```json
{
  "ok": false,
  "error": "user_not_found"
}
```

### `PUT /admin/users/{id}`

Updates one or more editable user fields. Fields that remain unchanged do not
need to be sent. `id`, `role`, and the token cannot be changed.

Example payload:

```json
{
  "name": "<NEW_NAME>",
  "enabled": <true|false>,
  "allow_remote": <true|false>,
  "schedules": [
    {
      "allowed_days": ["monday", "friday"],
      "start_time": "08:00",
      "end_time": "12:00"
    },
    {
      "allowed_days": ["saturday", "sunday"],
      "start_time": "10:00",
      "end_time": "14:00"
    }
  ]
}
```

Response `200 OK`: updated user representation, without a token.

### `DELETE /admin/users/{id}`

Permanently deletes a user.

Payload: no body.

Response `204 No Content`: no body.

## Common error responses

Another Bluetooth operation is already running (`409 Conflict`):

```json
{
  "ok": false,
  "error": "device_busy"
}
```

This applies to all device endpoints, including `GET /apt_door/status`. The
request is rejected before invoking the device and is not queued. Health,
capabilities, and administration requests remain available. Avoid overlapping
device requests; pause status polling while sending a door command.

An operation exceeding the configured timeout returns `504 Gateway Timeout`
with `{"ok":false,"error":"command timed out"}`. Connection cleanup can add up
to three seconds. An error or timeout after a physical command was transmitted
does not prove that the door did not move; check its state before deciding
whether to issue another command.

Missing or invalid bearer token:

```text
HTTP 401 Unauthorized
WWW-Authenticate: Bearer
<no body>
```

A regular user attempting to access `/admin/*`:

```json
{
  "ok": false,
  "error": "admin_required"
}
```

Operation denied by permissions:

```json
{
  "ok": false,
  "error": "<REASON>",
  "reason": "<REASON>",
  "distance_m": <DISTANCE_METERS>
}
```

`<REASON>` can be `user_disabled`, `outside_allowed_schedule`, or
`remote_access_not_allowed`.

Missing or invalid required coordinates for a user operation:

```json
{
  "ok": false,
  "error": "GPS coordinates are required and must be valid"
}
```

Invalid administrative payload:

```json
{
  "ok": false,
  "error": "<INVALID_FIELD_DESCRIPTION>"
}
```

Device operation failed:

```json
{
  "ok": false,
  "error": "<FAILURE_DESCRIPTION>"
}
```
