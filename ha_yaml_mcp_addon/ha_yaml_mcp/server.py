import os
import json
import secrets
import re
import hashlib
import difflib
import threading
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

CHANGE_DIR = Path("/data/automation_changes")
MAX_PROPOSALS = 100
MAX_CONFIG_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024 * 1024
CHANGE_LOCK = threading.Lock()

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


@mcp.tool()
def get_automation_config(automation_id: str) -> dict:
    """
    Retrieve an existing automation through the native
    Home Assistant configuration API.

    Supply the automation configuration ID from automations.yaml,
    not the automation entity ID.

    Returns the current automation configuration.
    This tool is strictly read-only and cannot save, modify,
    delete or execute automations.
    """

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", automation_id):
        raise ValueError("Invalid automation configuration ID")

    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")

    if not supervisor_token:
        raise RuntimeError("Home Assistant API access is unavailable")

    url = (
        "http://supervisor/core/api/config/automation/config/"
        + quote(automation_id, safe="")
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
            payload = response.read(1024 * 1024 + 1)

    except HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(
                "Automation configuration not found"
            ) from None

        raise RuntimeError(
            f"Home Assistant configuration API returned HTTP {exc.code}"
        ) from None

    except URLError:
        raise RuntimeError(
            "Unable to reach Home Assistant configuration API"
        ) from None

    if len(payload) > 1024 * 1024:
        raise ValueError("Automation configuration too large")

    result = json.loads(payload.decode("utf-8"))

    if not isinstance(result, dict):
        raise RuntimeError("Unexpected automation configuration response")

    return result


def canonical_config(config: dict) -> bytes:
    return json.dumps(
        config,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def config_fingerprint(config: dict) -> str:
    return hashlib.sha256(
        canonical_config(config)
    ).hexdigest()


def readable_config(config: dict) -> str:
    return json.dumps(
        config,
        sort_keys=True,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )


def ensure_change_directory() -> Path:
    if CHANGE_DIR.is_symlink():
        raise RuntimeError("Unsafe proposal storage directory")

    CHANGE_DIR.mkdir(
        mode=0o700,
        parents=True,
        exist_ok=True,
    )

    if not CHANGE_DIR.is_dir():
        raise RuntimeError("Proposal storage unavailable")

    if CHANGE_DIR.resolve().parent != Path("/data").resolve():
        raise RuntimeError("Unsafe proposal storage location")

    CHANGE_DIR.chmod(0o700)

    return CHANGE_DIR


def validate_proposed_config(
    automation_id: str,
    proposed_config: dict,
) -> dict:
    """
    Perform basic structural checks only.

    This is not native Home Assistant schema validation.
    """

    if not isinstance(proposed_config, dict):
        raise ValueError("Proposed configuration must be an object")

    if len(canonical_config(proposed_config)) > MAX_CONFIG_BYTES:
        raise ValueError("Proposed configuration too large")

    proposed = json.loads(
        canonical_config(proposed_config).decode("utf-8")
    )

    if "id" in proposed:
        if str(proposed["id"]) != automation_id:
            raise ValueError("Automation ID cannot be changed")

    proposed["id"] = automation_id

    alias = proposed.get("alias")

    if not isinstance(alias, str) or not alias.strip():
        raise ValueError("A non-empty automation alias is required")

    for singular, plural in (
        ("trigger", "triggers"),
        ("condition", "conditions"),
        ("action", "actions"),
    ):
        if singular in proposed and plural in proposed:
            raise ValueError(
                f"Do not supply both {singular} and {plural}"
            )

    triggers = proposed.get(
        "triggers",
        proposed.get("trigger"),
    )

    actions = proposed.get(
        "actions",
        proposed.get("action"),
    )

    conditions = proposed.get(
        "conditions",
        proposed.get("condition", []),
    )

    if not isinstance(triggers, list):
        raise ValueError("Triggers must be supplied as a list")

    if not isinstance(actions, list) or not actions:
        raise ValueError("A non-empty actions list is required")

    if not isinstance(conditions, list):
        raise ValueError("Conditions must be supplied as a list")

    for section in (triggers, actions, conditions):
        if not all(isinstance(item, dict) for item in section):
            raise ValueError(
                "Trigger, condition and action entries must be objects"
            )

    mode = proposed.get("mode", "single")

    if mode not in {"single", "restart", "queued", "parallel"}:
        raise ValueError("Invalid automation mode")

    if len(canonical_config(proposed)) > MAX_CONFIG_BYTES:
        raise ValueError("Proposed configuration too large")

    return proposed


def make_config_diff(
    original: dict,
    proposed: dict,
) -> str:
    return "\n".join(
        difflib.unified_diff(
            readable_config(original).splitlines(),
            readable_config(proposed).splitlines(),
            fromfile="current_configuration",
            tofile="proposed_configuration",
            lineterm="",
        )
    )


def store_change(record: dict) -> None:
    directory = ensure_change_directory()

    change_id = record["change_id"]

    if not re.fullmatch(r"[a-f0-9]{32}", change_id):
        raise ValueError("Invalid change reference")

    destination = directory / f"{change_id}.json"

    payload = json.dumps(
        record,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ).encode("utf-8")

    if len(payload) > MAX_RECORD_BYTES:
        raise ValueError("Proposal record too large")

    with CHANGE_LOCK:
        existing = list(directory.glob("*.json"))

        if len(existing) >= MAX_PROPOSALS:
            raise RuntimeError(
                "Proposal storage limit reached. "
                "No existing backups have been deleted."
            )

        temporary = directory / (
            f".{change_id}.{secrets.token_hex(8)}.tmp"
        )

        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
        )

        try:
            fd = os.open(temporary, flags, 0o600)

            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())

            os.link(temporary, destination)

            directory_fd = os.open(
                directory,
                os.O_RDONLY | os.O_DIRECTORY,
            )

            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

        finally:
            temporary.unlink(missing_ok=True)


def load_change(change_id: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", change_id):
        raise ValueError("Invalid change reference")

    directory = ensure_change_directory()

    path = safe_file(
        directory,
        f"{change_id}.json",
        frozenset({f"{change_id}.json"}),
    )

    if path.stat().st_size > MAX_RECORD_BYTES:
        raise ValueError("Proposal record too large")

    record = json.loads(
        path.read_text(encoding="utf-8")
    )

    if not isinstance(record, dict):
        raise RuntimeError("Invalid proposal record")

    if record.get("change_id") != change_id:
        raise RuntimeError("Proposal reference mismatch")

    if record.get("schema_version") != 1:
        raise RuntimeError("Unsupported proposal record version")

    if record.get("status") != "prepared_only":
        raise RuntimeError("Unexpected proposal status")

    original = record.get("original_config")
    proposed = record.get("proposed_config")

    if not isinstance(original, dict):
        raise RuntimeError("Original configuration missing")

    if not isinstance(proposed, dict):
        raise RuntimeError("Proposed configuration missing")

    if config_fingerprint(original) != record.get(
        "original_fingerprint"
    ):
        raise RuntimeError("Original configuration integrity check failed")

    if config_fingerprint(proposed) != record.get(
        "proposed_fingerprint"
    ):
        raise RuntimeError("Proposed configuration integrity check failed")

    return record


@mcp.tool()
def prepare_automation_change(
    automation_id: str,
    proposed_config: dict,
    reason: str,
) -> dict:
    """
    Prepare a proposed replacement for an existing automation.

    Supply the complete proposed automation configuration,
    not a partial patch, and a reason for the change.

    Retrieves the original through the native Home Assistant API,
    performs basic structural checks, calculates fingerprints,
    generates a comparison and stores both configurations in
    persistent app storage.

    Does not modify, reload, execute or delete any automation.

    Native Home Assistant schema validation is not performed.
    A prepared proposal is not an approved change.
    """

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", automation_id):
        raise ValueError("Invalid automation configuration ID")

    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("A reason for the proposed change is required")

    if len(reason) > 2000:
        raise ValueError("Change reason too long")

    proposed = validate_proposed_config(
        automation_id,
        proposed_config,
    )

    original = get_automation_config(automation_id)

    if str(original.get("id", "")) != automation_id:
        raise RuntimeError("Original automation ID mismatch")

    original_hash = config_fingerprint(original)
    proposed_hash = config_fingerprint(proposed)

    if original_hash == proposed_hash:
        return {
            "status": "no_changes",
            "automation_id": automation_id,
            "alias": original.get("alias"),
            "live_configuration_modified": False,
        }

    comparison = make_config_diff(
        original,
        proposed,
    )

    change_id = secrets.token_hex(16)

    record = {
        "schema_version": 1,
        "change_id": change_id,
        "automation_id": automation_id,
        "alias": original.get("alias"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason.strip(),
        "status": "prepared_only",
        "approved": False,
        "native_validation_performed": False,
        "original_fingerprint": original_hash,
        "proposed_fingerprint": proposed_hash,
        "original_config": original,
        "proposed_config": proposed,
        "diff": comparison,
    }

    store_change(record)

    return {
        "status": "prepared_only",
        "change_id": change_id,
        "automation_id": automation_id,
        "alias": original.get("alias"),
        "created_at": record["created_at"],
        "reason": record["reason"],
        "original_fingerprint": original_hash,
        "proposed_fingerprint": proposed_hash,
        "backup_stored": True,
        "structural_checks_passed": True,
        "native_validation_performed": False,
        "approved": False,
        "live_configuration_modified": False,
        "diff": comparison,
    }


@mcp.tool()
def review_automation_change(
    change_id: str,
) -> dict:
    """
    Review a previously prepared automation change.

    Returns the original and proposed configurations,
    fingerprints, comparison and backup information.

    Also retrieves the current live configuration and
    checks whether it has changed since preparation.

    Does not approve, apply, modify or execute anything.
    """

    record = load_change(change_id)

    original = record["original_config"]
    proposed = record["proposed_config"]

    comparison = make_config_diff(
        original,
        proposed,
    )

    result = {
        "status": "prepared_only",
        "change_id": record["change_id"],
        "automation_id": record["automation_id"],
        "alias": record.get("alias"),
        "created_at": record["created_at"],
        "reason": record["reason"],
        "approved": False,
        "native_validation_performed": False,
        "backup_integrity_verified": True,
        "original_fingerprint": record["original_fingerprint"],
        "proposed_fingerprint": record["proposed_fingerprint"],
        "original_config": original,
        "proposed_config": proposed,
        "diff": comparison,
        "live_configuration_modified": False,
    }

    try:
        current = get_automation_config(
            record["automation_id"]
        )

        current_hash = config_fingerprint(current)

        result["current_fingerprint"] = current_hash
        result["conflict_detected"] = (
            current_hash != record["original_fingerprint"]
        )

        result["live_check_completed"] = True

    except Exception:
        result["live_check_completed"] = False
        result["conflict_detected"] = None
        result["warning"] = (
            "Unable to verify the current live configuration. "
            "Do not treat this proposal as safe to apply."
        )

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
