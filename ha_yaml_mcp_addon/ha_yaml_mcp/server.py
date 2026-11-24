import os
import json
import secrets
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode, quote
from urllib.error import HTTPError, URLError

import websocket

from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

CONFIG = Path(os.environ.get("HA_CONFIG_DIR", "/homeassistant")).resolve()

ALLOWED = frozenset({
    "configuration.yaml",
    "automations.yaml",
    "scripts.yaml",
    "scenes.yaml",
    "groups.yaml",
    "customize.yaml",
    "templates.yaml",
})

DASHBOARD_FILES = frozenset({
    "lovelace.dashboard_home",
    "lovelace.dashboard_climate",
    "lovelace.dashboard_cameras",
    "lovelace.dashboard_redo",
    "lovelace.iberter_remote",
    "lovelace_dashboards",
})

TOKEN = os.environ.get("HA_YAML_MCP_TOKEN", "")
if len(TOKEN) < 32:
    raise RuntimeError("HA_YAML_MCP_TOKEN must be at least 32 characters")

mcp = FastMCP(
    "Home Assistant YAML (read-only)",
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
)


def safe_file(directory: Path, filename: str, allowlist: frozenset) -> Path:
    if filename not in allowlist:
        raise ValueError("File is not on the read-only allowlist")
    p = directory / filename
    if p.is_symlink() or p.resolve().parent != directory:
        raise ValueError("Unsafe path")
    if not p.is_file():
        raise FileNotFoundError(filename)
    return p


@mcp.tool()
def list_yaml_files() -> list[str]:
    return sorted(
        name for name in ALLOWED
        if (CONFIG / name).is_file()
        and not (CONFIG / name).is_symlink()
    )


@mcp.tool()
def read_yaml_file(filename: str) -> str:
    p = safe_file(CONFIG, filename, ALLOWED)
    if p.stat().st_size > 1024 * 1024:
        raise ValueError("File too large")
    return p.read_text(encoding="utf-8")


@mcp.tool()
def list_dashboards() -> list[str]:
    directory = CONFIG / ".storage"
    return sorted(
        name for name in DASHBOARD_FILES
        if (directory / name).is_file()
        and not (directory / name).is_symlink()
    )


@mcp.tool()
def read_dashboard(filename: str) -> str:
    directory = (CONFIG / ".storage").resolve()
    p = safe_file(directory, filename, DASHBOARD_FILES)
    if p.stat().st_size > 1024 * 1024:
        raise ValueError("Dashboard file too large")
    return p.read_text(encoding="utf-8")


def template_entries() -> list:
    directory = (CONFIG / ".storage").resolve()
    p = safe_file(
        directory,
        "core.config_entries",
        frozenset({"core.config_entries"}),
    )

    if p.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Configuration entries file too large")

    content = json.loads(p.read_text(encoding="utf-8"))

    return [
        entry
        for entry in content.get("data", {}).get("entries", [])
        if entry.get("domain") == "template"
    ]


@mcp.tool()
def list_template_helpers() -> list[dict]:
    """List UI-created Home Assistant template helpers."""
    return [
        {
            "entry_id": entry.get("entry_id"),
            "title": entry.get("title"),
            "disabled_by": entry.get("disabled_by"),
        }
        for entry in template_entries()
    ]


@mcp.tool()
def read_template_helper(entry_id: str) -> dict:
    """Read the configuration and Jinja templates of a template helper."""
    for entry in template_entries():
        if entry.get("entry_id") == entry_id:
            return {
                "entry_id": entry.get("entry_id"),
                "title": entry.get("title"),
                "data": entry.get("data", {}),
                "options": entry.get("options", {}),
            }

    raise ValueError("Template helper not found")


def parse_timestamp(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Use an ISO 8601 timestamp")

    if dt.tzinfo is None:
        raise ValueError("Timestamp must include a timezone offset")

    return dt.astimezone(timezone.utc)


@mcp.tool()
def get_entity_history(
    entity_ids: list[str],
    start_time: str,
    end_time: str = "",
    include_attributes: bool = True,
) -> list:
    """
    Retrieve detailed recorded Home Assistant entity history.

    Returns actual state records and timestamps rather than
    aggregated numeric statistics.

    Supports climate entities, binary sensors, switches,
    automations and other recorded entities.

    Timestamps must be ISO 8601 with a timezone offset.
    Maximum 10 entities and 24 hours per request.
    """

    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")

    if not supervisor_token:
        raise RuntimeError("Home Assistant API access is unavailable")

    if not entity_ids or len(entity_ids) > 10:
        raise ValueError("Supply between 1 and 10 entity IDs")

    for entity_id in entity_ids:
        if not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", entity_id):
            raise ValueError("Invalid entity ID")

    start = parse_timestamp(start_time)
    end = (
        parse_timestamp(end_time)
        if end_time
        else datetime.now(timezone.utc)
    )

    if end <= start:
        raise ValueError("End time must be after start time")

    if end - start > timedelta(hours=24):
        raise ValueError("Maximum history period is 24 hours")

    params = {
        "filter_entity_id": ",".join(entity_ids),
        "end_time": end.isoformat(),
    }

    if not include_attributes:
        params["no_attributes"] = "true"

    url = (
        "http://supervisor/core/api/history/period/"
        + quote(start.isoformat(), safe="")
        + "?"
        + urlencode(params)
    )

    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {supervisor_token}",
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=30) as response:
            payload = response.read(4 * 1024 * 1024 + 1)

    except HTTPError as exc:
        raise RuntimeError(
            f"Home Assistant history API returned HTTP {exc.code}"
        ) from None

    except URLError:
        raise RuntimeError(
            "Unable to reach Home Assistant history API"
        ) from None

    if len(payload) > 4 * 1024 * 1024:
        raise ValueError(
            "History response too large. Request fewer entities "
            "or a shorter time period."
        )

    return json.loads(payload.decode("utf-8"))


def receive_ws_message(connection) -> dict:
    raw = connection.recv()

    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("WebSocket response too large")

    message = json.loads(raw)

    if not isinstance(message, dict):
        raise RuntimeError("Unexpected WebSocket response")

    return message


def trace_request(
    command: str,
    automation_id: str,
    run_id: str = "",
):
    """Execute an allowlisted read-only automation trace request."""

    if command not in {"trace/list", "trace/get"}:
        raise ValueError("WebSocket command not permitted")

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", automation_id):
        raise ValueError("Invalid automation configuration ID")

    if command == "trace/get":
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            raise ValueError("Invalid trace run ID")

    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")

    if not supervisor_token:
        raise RuntimeError("Home Assistant API access is unavailable")

    connection = None

    try:
        connection = websocket.create_connection(
            "ws://supervisor/core/websocket",
            timeout=20,
            header=[
                f"Authorization: Bearer {supervisor_token}"
            ],
        )

        greeting = receive_ws_message(connection)

        if greeting.get("type") == "auth_required":
            connection.send(json.dumps({
                "type": "auth",
                "access_token": supervisor_token,
            }))

            authentication = receive_ws_message(connection)

            if authentication.get("type") != "auth_ok":
                raise RuntimeError(
                    "Home Assistant WebSocket authentication failed"
                )

        elif greeting.get("type") != "auth_ok":
            raise RuntimeError(
                "Unexpected Home Assistant WebSocket greeting"
            )

        request = {
            "id": 1,
            "type": command,
            "domain": "automation",
            "item_id": automation_id,
        }

        if command == "trace/get":
            request["run_id"] = run_id

        connection.send(json.dumps(request))

        for _ in range(20):
            response = receive_ws_message(connection)

            if response.get("id") != 1:
                continue

            if not response.get("success"):
                error = response.get("error", {})
                raise RuntimeError(
                    "Home Assistant trace request failed: "
                    + str(error.get("code", "unknown_error"))
                )

            return response.get("result")

        raise RuntimeError("No matching trace response received")

    except websocket.WebSocketException:
        raise RuntimeError(
            "Unable to complete Home Assistant WebSocket request"
        ) from None

    except OSError:
        raise RuntimeError(
            "Unable to reach Home Assistant WebSocket API"
        ) from None

    finally:
        if connection is not None:
            connection.close()


@mcp.tool()
def list_automation_traces(automation_id: str) -> list:
    """
    List retained execution traces for a Home Assistant automation.

    Supply the automation configuration ID from automations.yaml,
    not the automation entity ID.

    Returns available trace run IDs and execution metadata.
    Only traces retained by Home Assistant are available.
    """

    result = trace_request(
        "trace/list",
        automation_id,
    )

    if not isinstance(result, list):
        raise RuntimeError("Unexpected automation trace list response")

    return result


@mcp.tool()
def get_automation_trace(
    automation_id: str,
    run_id: str,
) -> dict:
    """
    Retrieve a retained automation execution trace.

    Supply the automation configuration ID from automations.yaml
    and a run ID returned by list_automation_traces.

    Returns recorded triggers, conditions, actions, variables,
    execution paths and errors.

    This tool cannot modify or execute automations.
    Trace contents may contain sensitive runtime information.
    """

    result = trace_request(
        "trace/get",
        automation_id,
        run_id,
    )

    if not isinstance(result, dict):
        raise RuntimeError("Unexpected automation trace response")

    return result


class TokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        authorization = request.headers.get("authorization", "")
        expected = f"Bearer {TOKEN}"

        if not secrets.compare_digest(authorization, expected):
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
            )

        return await call_next(request)


app = mcp.streamable_http_app()
app.add_middleware(TokenMiddleware)
