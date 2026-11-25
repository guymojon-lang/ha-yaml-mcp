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


CONFIG = Path(
    os.environ.get("HA_CONFIG_DIR", "/homeassistant")
).resolve()

CHANGE_DIR = Path("/data/automation_changes")
APPROVAL_DIR = Path("/data/used_approvals")
OPTIONS_FILE = Path("/data/options.json")


HELPER_DOMAINS = frozenset({"input_boolean", "input_number", "input_text", "input_select", "input_datetime", "input_button", "counter", "timer", "schedule", "zone", "person"})
MAX_PROPOSALS = 100
MAX_CONFIG_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024 * 1024
APPROVAL_MINUTES = 60

CHANGE_LOCK = threading.Lock()
EDIT_LOCK = threading.Lock()

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
    raise RuntimeError(
        "HA_YAML_MCP_TOKEN must be at least 32 characters"
    )

mcp = FastMCP(
    "Home Assistant YAML",
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
)


def safe_file(directory, filename, allowlist):
    if filename not in allowlist:
        raise ValueError("File is not on the allowlist")

    p = directory / filename

    if p.is_symlink() or p.resolve().parent != directory:
        raise ValueError("Unsafe path")

    if not p.is_file():
        raise FileNotFoundError(filename)

    return p


def supervisor_token():
    token = os.environ.get("SUPERVISOR_TOKEN", "")

    if not token:
        raise RuntimeError(
            "Home Assistant API access is unavailable"
        )

    return token


@mcp.tool()
def list_yaml_files() -> list[str]:
    """List permitted Home Assistant YAML files."""

    return sorted(
        name for name in ALLOWED
        if (CONFIG / name).is_file()
        and not (CONFIG / name).is_symlink()
    )


@mcp.tool()
def read_yaml_file(filename: str) -> str:
    """Read an allowlisted Home Assistant YAML file."""

    p = safe_file(CONFIG, filename, ALLOWED)

    if p.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError("File too large")

    return p.read_text(encoding="utf-8")


@mcp.tool()
def list_dashboards() -> list[str]:
    """List permitted dashboard configuration files."""

    directory = CONFIG / ".storage"

    return sorted(
        name for name in DASHBOARD_FILES
        if (directory / name).is_file()
        and not (directory / name).is_symlink()
    )


@mcp.tool()
def read_dashboard(filename: str) -> str:
    """Read an allowlisted dashboard configuration."""

    directory = (CONFIG / ".storage").resolve()

    p = safe_file(
        directory,
        filename,
        DASHBOARD_FILES,
    )

    if p.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError("Dashboard file too large")

    return p.read_text(encoding="utf-8")


def template_entries():
    directory = (CONFIG / ".storage").resolve()

    p = safe_file(
        directory,
        "core.config_entries",
        frozenset({"core.config_entries"}),
    )

    if p.stat().st_size > 16 * 1024 * 1024:
        raise ValueError(
            "Configuration entries file too large"
        )

    content = json.loads(
        p.read_text(encoding="utf-8")
    )

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
    """Read configuration and Jinja templates of a helper."""

    for entry in template_entries():
        if entry.get("entry_id") == entry_id:
            return {
                "entry_id": entry.get("entry_id"),
                "title": entry.get("title"),
                "data": entry.get("data", {}),
                "options": entry.get("options", {}),
            }

    raise ValueError("Template helper not found")


def parse_timestamp(value):
    try:
        dt = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except ValueError:
        raise ValueError(
            "Use an ISO 8601 timestamp"
        ) from None

    if dt.tzinfo is None:
        raise ValueError(
            "Timestamp must include a timezone offset"
        )

    return dt.astimezone(timezone.utc)


@mcp.tool()
def get_entity_history(
    entity_ids: list[str],
    start_time: str,
    end_time: str = "",
    include_attributes: bool = True,
) -> list:
    """
    Retrieve detailed recorded entity history.

    Returns actual state records and timestamps.
    Maximum 10 entities and 24 hours per request.
    Timestamps must include a timezone offset.
    """

    token = supervisor_token()

    if not entity_ids or len(entity_ids) > 10:
        raise ValueError(
            "Supply between 1 and 10 entity IDs"
        )

    for entity_id in entity_ids:
        if not re.fullmatch(
            r"[a-z0-9_]+\.[a-z0-9_]+",
            entity_id,
        ):
            raise ValueError("Invalid entity ID")

    start = parse_timestamp(start_time)

    end = (
        parse_timestamp(end_time)
        if end_time
        else datetime.now(timezone.utc)
    )

    if end <= start:
        raise ValueError(
            "End time must be after start time"
        )

    if end - start > timedelta(hours=24):
        raise ValueError(
            "Maximum history period is 24 hours"
        )

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
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=30) as response:
            payload = response.read(
                4 * 1024 * 1024 + 1
            )

    except HTTPError as exc:
        raise RuntimeError(
            f"History API returned HTTP {exc.code}"
        ) from None

    except URLError:
        raise RuntimeError(
            "Unable to reach Home Assistant history API"
        ) from None

    if len(payload) > 4 * 1024 * 1024:
        raise ValueError(
            "History response too large. "
            "Request fewer entities or a shorter period."
        )

    return json.loads(payload.decode("utf-8"))


def receive_ws_message(connection):
    raw = connection.recv()

    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("WebSocket response too large")

    message = json.loads(raw)

    if not isinstance(message, dict):
        raise RuntimeError(
            "Unexpected WebSocket response"
        )

    return message


def websocket_request(request):
    allowed_commands = {
        "trace/list",
        "trace/get",
        "validate_config",
        "lovelace/dashboards/list", "lovelace/config", "lovelace/config/save",
    } | {f"{domain}/{op}" for domain in HELPER_DOMAINS for op in ("list", "update")}

    if request.get("type") not in allowed_commands:
        raise ValueError(
            "WebSocket command not permitted"
        )

    token = supervisor_token()
    connection = None

    try:
        connection = websocket.create_connection(
            "ws://supervisor/core/websocket",
            timeout=20,
            header=[
                f"Authorization: Bearer {token}"
            ],
        )

        greeting = receive_ws_message(connection)

        if greeting.get("type") == "auth_required":
            connection.send(json.dumps({
                "type": "auth",
                "access_token": token,
            }))

            authentication = receive_ws_message(
                connection
            )

            if authentication.get("type") != "auth_ok":
                raise RuntimeError(
                    "WebSocket authentication failed"
                )

        elif greeting.get("type") != "auth_ok":
            raise RuntimeError(
                "Unexpected WebSocket greeting"
            )

        message = dict(request)
        message["id"] = 1

        connection.send(json.dumps(message))

        for _ in range(20):
            response = receive_ws_message(connection)

            if response.get("id") != 1:
                continue

            if response.get("success") is not True:
                raise RuntimeError(
                    "Home Assistant WebSocket request failed: "
                    + json.dumps(
                        response.get("error", {})
                    )
                )

            return response.get("result")

        raise RuntimeError(
            "No matching WebSocket response received"
        )

    except websocket.WebSocketException:
        raise RuntimeError(
            "Unable to complete WebSocket request"
        ) from None

    except OSError:
        raise RuntimeError(
            "Unable to reach Home Assistant WebSocket API"
        ) from None

    finally:
        if connection is not None:
            connection.close()


def trace_request(
    command,
    automation_id,
    run_id="",
):
    if command not in {"trace/list", "trace/get"}:
        raise ValueError("Trace command not permitted")

    if not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}",
        automation_id,
    ):
        raise ValueError(
            "Invalid automation configuration ID"
        )

    request = {
        "type": command,
        "domain": "automation",
        "item_id": automation_id,
    }

    if command == "trace/get":
        if not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}",
            run_id,
        ):
            raise ValueError("Invalid trace run ID")

        request["run_id"] = run_id

    return websocket_request(request)


@mcp.tool()
def list_automation_traces(
    automation_id: str,
) -> list:
    """
    List retained automation execution traces.

    Supply the automation configuration ID,
    not the automation entity ID.
    """

    result = trace_request(
        "trace/list",
        automation_id,
    )

    if not isinstance(result, list):
        raise RuntimeError(
            "Unexpected automation trace list response"
        )

    return result


@mcp.tool()
def get_automation_trace(
    automation_id: str,
    run_id: str,
) -> dict:
    """
    Retrieve a retained automation execution trace.

    Returns triggers, conditions, actions, variables,
    execution paths and errors.
    """

    result = trace_request(
        "trace/get",
        automation_id,
        run_id,
    )

    if not isinstance(result, dict):
        raise RuntimeError(
            "Unexpected automation trace response"
        )

    return result


@mcp.tool()
def get_automation_config(
    automation_id: str,
) -> dict:
    """
    Retrieve an existing automation through the native
    Home Assistant configuration API.

    Supply the automation configuration ID.
    This tool is strictly read-only.
    """

    if not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}",
        automation_id,
    ):
        raise ValueError(
            "Invalid automation configuration ID"
        )

    token = supervisor_token()

    url = (
        "http://supervisor/core/api/config/"
        "automation/config/"
        + quote(automation_id, safe="")
    )

    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=30) as response:
            payload = response.read(
                MAX_CONFIG_BYTES + 1
            )

    except HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(
                "Automation configuration not found"
            ) from None

        raise RuntimeError(
            f"Configuration API returned HTTP {exc.code}"
        ) from None

    except URLError:
        raise RuntimeError(
            "Unable to reach configuration API"
        ) from None

    if len(payload) > MAX_CONFIG_BYTES:
        raise ValueError(
            "Automation configuration too large"
        )

    result = json.loads(payload.decode("utf-8"))

    if not isinstance(result, dict):
        raise RuntimeError(
            "Unexpected automation configuration response"
        )

    return result


def canonical_config(config):
    return json.dumps(
        config,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def config_fingerprint(config):
    return hashlib.sha256(
        canonical_config(config)
    ).hexdigest()


def readable_config(config):
    return json.dumps(
        config,
        sort_keys=True,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )


def ensure_private_directory(directory):
    if directory.is_symlink():
        raise RuntimeError(
            "Unsafe persistent storage directory"
        )

    directory.mkdir(
        mode=0o700,
        parents=True,
        exist_ok=True,
    )

    if not directory.is_dir():
        raise RuntimeError(
            "Persistent storage unavailable"
        )

    if directory.resolve().parent != Path("/data").resolve():
        raise RuntimeError(
            "Unsafe persistent storage location"
        )

    directory.chmod(0o700)

    return directory


def ensure_change_directory():
    return ensure_private_directory(CHANGE_DIR)


def validate_proposed_config(
    automation_id,
    proposed_config,
):
    """
    Basic structural validation only.
    Native validation is performed separately.
    """

    if not isinstance(proposed_config, dict):
        raise ValueError(
            "Proposed configuration must be an object"
        )

    if len(canonical_config(proposed_config)) > MAX_CONFIG_BYTES:
        raise ValueError(
            "Proposed configuration too large"
        )

    proposed = json.loads(
        canonical_config(proposed_config)
    )

    if "id" in proposed:
        if str(proposed["id"]) != automation_id:
            raise ValueError(
                "Automation ID cannot be changed"
            )

    proposed["id"] = automation_id

    alias = proposed.get("alias")

    if not isinstance(alias, str) or not alias.strip():
        raise ValueError(
            "A non-empty automation alias is required"
        )

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
        raise ValueError(
            "Triggers must be supplied as a list"
        )

    if not isinstance(actions, list) or not actions:
        raise ValueError(
            "A non-empty actions list is required"
        )

    if not isinstance(conditions, list):
        raise ValueError(
            "Conditions must be supplied as a list"
        )

    for section in (
        triggers,
        actions,
        conditions,
    ):
        if not all(
            isinstance(item, dict)
            for item in section
        ):
            raise ValueError(
                "Configuration entries must be objects"
            )

    mode = proposed.get("mode", "single")

    if mode not in {
        "single",
        "restart",
        "queued",
        "parallel",
    }:
        raise ValueError("Invalid automation mode")

    if len(canonical_config(proposed)) > MAX_CONFIG_BYTES:
        raise ValueError(
            "Proposed configuration too large"
        )

    return proposed


def make_config_diff(original, proposed):
    return "\n".join(
        difflib.unified_diff(
            readable_config(original).splitlines(),
            readable_config(proposed).splitlines(),
            fromfile="current_configuration",
            tofile="proposed_configuration",
            lineterm="",
        )
    )


def sync_directory(directory):
    fd = os.open(
        directory,
        os.O_RDONLY | os.O_DIRECTORY,
    )

    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def store_change(record):
    directory = ensure_change_directory()

    change_id = record["change_id"]

    if not re.fullmatch(
        r"[a-f0-9]{32}",
        change_id,
    ):
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
            fd = os.open(
                temporary,
                flags,
                0o600,
            )

            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())

            os.link(temporary, destination)

            sync_directory(directory)

        finally:
            temporary.unlink(missing_ok=True)


def load_change(change_id, resource="automation"):
    if not re.fullmatch(
        r"[a-f0-9]{32}",
        change_id,
    ):
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
        raise RuntimeError(
            "Proposal reference mismatch"
        )

    if record.get("schema_version") != 1:
        raise RuntimeError(
            "Unsupported proposal record version"
        )

    if record.get("status") != "prepared_only":
        raise RuntimeError(
            "Unexpected proposal status"
        )

    if record.get("resource", "automation") != resource:
        raise ValueError("Wrong proposal resource type")

    original = record.get("original_config")
    proposed = record.get("proposed_config")

    if not isinstance(original, dict):
        raise RuntimeError(
            "Original configuration missing"
        )

    if not isinstance(proposed, dict):
        raise RuntimeError(
            "Proposed configuration missing"
        )

    if config_fingerprint(original) != record.get(
        "original_fingerprint"
    ):
        raise RuntimeError(
            "Original configuration integrity check failed"
        )

    if config_fingerprint(proposed) != record.get(
        "proposed_fingerprint"
    ):
        raise RuntimeError(
            "Proposed configuration integrity check failed"
        )

    return record


def create_change_record(
    automation_id,
    original,
    proposed,
    reason,
):
    return {
        "schema_version": 1,
        "write_version": 4,
        "change_id": secrets.token_hex(16),
        "automation_id": automation_id,
        "alias": original.get("alias"),
        "created_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "reason": reason.strip(),
        "status": "prepared_only",
        "approved": False,
        "native_validation_performed": False,
        "original_fingerprint": config_fingerprint(
            original
        ),
        "proposed_fingerprint": config_fingerprint(
            proposed
        ),
        "original_config": original,
        "proposed_config": proposed,
        "diff": make_config_diff(
            original,
            proposed,
        ),
    }


@mcp.tool()
def prepare_automation_change(
    automation_id: str,
    proposed_config: dict,
    reason: str,
) -> dict:
    """
    Prepare a complete proposed replacement configuration.

    Stores the original and proposed configurations,
    fingerprints and comparison in persistent app storage.

    Does not modify, reload or execute automations.
    Native validation and approval are separate.
    """

    if not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}",
        automation_id,
    ):
        raise ValueError(
            "Invalid automation configuration ID"
        )

    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(
            "A reason for the change is required"
        )

    if len(reason) > 2000:
        raise ValueError("Change reason too long")

    proposed = validate_proposed_config(
        automation_id,
        proposed_config,
    )

    original = get_automation_config(
        automation_id
    )

    if str(original.get("id", "")) != automation_id:
        raise RuntimeError(
            "Original automation ID mismatch"
        )

    if config_fingerprint(original) == config_fingerprint(
        proposed
    ):
        return {
            "status": "no_changes",
            "automation_id": automation_id,
            "alias": original.get("alias"),
            "live_configuration_modified": False,
        }

    record = create_change_record(
        automation_id,
        original,
        proposed,
        reason,
    )

    store_change(record)

    return {
        "status": "prepared_only",
        "change_id": record["change_id"],
        "automation_id": automation_id,
        "alias": record["alias"],
        "created_at": record["created_at"],
        "reason": record["reason"],
        "original_fingerprint": record[
            "original_fingerprint"
        ],
        "proposed_fingerprint": record[
            "proposed_fingerprint"
        ],
        "backup_stored": True,
        "structural_checks_passed": True,
        "native_validation_performed": False,
        "approved": False,
        "live_configuration_modified": False,
        "diff": record["diff"],
    }


@mcp.tool()
def review_automation_change(
    change_id: str,
) -> dict:
    """
    Review a prepared automation change and backup.

    Compares the current live configuration with the
    original fingerprint to detect conflicts.

    Does not approve or apply changes.
    """

    record = load_change(change_id)

    original = record["original_config"]
    proposed = record["proposed_config"]

    result = {
        "status": "approval_consumed" if approval_used(record) else "prepared_only",
        "change_id": change_id,
        "automation_id": record["automation_id"],
        "alias": record.get("alias"),
        "created_at": record["created_at"],
        "reason": record["reason"],
        "approved": False,
        "native_validation_performed": False,
        "backup_integrity_verified": True,
        "original_fingerprint": record[
            "original_fingerprint"
        ],
        "proposed_fingerprint": record[
            "proposed_fingerprint"
        ],
        "original_config": original,
        "proposed_config": proposed,
        "diff": make_config_diff(
            original,
            proposed,
        ),
        "live_configuration_modified": False,
    }

    try:
        current = get_automation_config(
            record["automation_id"]
        )

        current_hash = config_fingerprint(current)

        result["current_fingerprint"] = current_hash

        result["conflict_detected"] = (
            current_hash
            != record["original_fingerprint"]
        )

        result["live_configuration_modified"] = current_hash != record["original_fingerprint"]
        result["matches_proposal"] = current_hash == record["proposed_fingerprint"]
        result["live_check_completed"] = True

    except Exception:
        result["live_check_completed"] = False
        result["conflict_detected"] = None
        result["warning"] = (
            "Unable to verify live configuration. "
            "Do not treat this proposal as safe to apply."
        )

    return result


# --------------------------------------------------
# CONTROLLED EDITING
# Version 0.4.0: existing automation editing.
# --------------------------------------------------


def native_validate(config):

    result = websocket_request({
        "type": "validate_config",
        "triggers": config.get("triggers", config.get("trigger", [])),
        "conditions": config.get("conditions", config.get("condition", [])),
        "actions": config.get("actions", config.get("action", [])),
    })

    if not isinstance(result, dict):
        raise RuntimeError(
            "Unexpected native validation result"
        )

    for section in (
        "triggers",
        "conditions",
        "actions",
    ):
        validation = result.get(section)

        if not isinstance(validation, dict):
            raise RuntimeError(
                f"No validation result for {section}: "
                + json.dumps(result)
            )

        if validation.get("valid") is not True:
            raise ValueError(
                f"Native validation failed for {section}: "
                + json.dumps(validation)
            )

    return result


def approval_reference(record):
    return (
        record["change_id"]
        + ":"
        + record["proposed_fingerprint"]
    )


def approval_expiry(record):
    return (
        parse_timestamp(record["created_at"])
        + timedelta(minutes=APPROVAL_MINUTES)
    )


def approval_used(record):
    directory = ensure_private_directory(
        APPROVAL_DIR
    )

    marker = directory / (
        record["change_id"] + ".json"
    )

    return marker.exists() or marker.is_symlink()


def check_approval(record):
    if record.get("write_version") != 4:
        raise PermissionError("Prepare a new proposal under version 0.4.0")
    if approval_used(record):
        raise PermissionError(
            "This approval has already been consumed"
        )

    if OPTIONS_FILE.is_symlink():
        raise RuntimeError(
            "Unsafe app configuration file"
        )

    if not OPTIONS_FILE.is_file():
        raise RuntimeError(
            "App configuration options unavailable"
        )

    if OPTIONS_FILE.stat().st_size > 65536:
        raise RuntimeError(
            "App configuration file too large"
        )

    options = json.loads(
        OPTIONS_FILE.read_text(
            encoding="utf-8"
        )
    )

    approved = options.get(
        "approved_change",
        "",
    )

    expected = approval_reference(record)

    if not isinstance(approved, str):
        raise PermissionError(
            "Invalid approval configuration"
        )

    if not secrets.compare_digest(
        approved,
        expected,
    ):
        raise PermissionError(
            "Independent approval has not been supplied"
        )

    created = parse_timestamp(
        record["created_at"]
    )

    now = datetime.now(timezone.utc)

    if now < created:
        raise PermissionError(
            "Invalid proposal timestamp"
        )

    if now > approval_expiry(record):
        raise PermissionError(
            "Proposal has expired. Prepare a new proposal."
        )


def consume_approval(record):
    directory = ensure_private_directory(
        APPROVAL_DIR
    )

    change_id = record["change_id"]

    if not re.fullmatch(
        r"[a-f0-9]{32}",
        change_id,
    ):
        raise ValueError(
            "Invalid change reference"
        )

    marker = directory / f"{change_id}.json"

    payload = json.dumps({
        "change_id": change_id,
        "proposed_fingerprint": record[
            "proposed_fingerprint"
        ],
        "consumed_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }).encode("utf-8")

    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
    )

    try:
        fd = os.open(
            marker,
            flags,
            0o600,
        )

    except FileExistsError:
        raise PermissionError(
            "This approval has already been consumed"
        ) from None

    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())

    sync_directory(directory)


def save_native_config(
    automation_id,
    config,
):

    token = supervisor_token()

    url = (
        "http://supervisor/core/api/config/"
        "automation/config/"
        + quote(automation_id, safe="")
    )

    payload = dict(config)
    payload.pop("id", None)

    request = Request(
        url,
        data=json.dumps(
            payload,
            allow_nan=False,
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read(
                MAX_CONFIG_BYTES + 1
            )

    except HTTPError as exc:
        raise RuntimeError(
            f"Native save returned HTTP {exc.code}. "
            "Check live configuration before retrying."
        ) from None

    except URLError:
        raise RuntimeError(
            "Native save connection failed. "
            "Save outcome may be uncertain."
        ) from None

    if len(raw) > MAX_CONFIG_BYTES:
        raise RuntimeError(
            "Save response too large"
        )

    return json.loads(raw.decode("utf-8"))


@mcp.tool()
def validate_automation_change(
    change_id: str,
) -> dict:
    """
    Validate a prepared automation change.

    Uses native Home Assistant WebSocket validation.
    Returns an independent approval reference.

    Does not save, approve or execute the automation.
    """

    record = load_change(change_id)


    proposed = record["proposed_config"]


    if approval_used(record):
        raise PermissionError(
            "This proposal has already been consumed"
        )

    if datetime.now(timezone.utc) > approval_expiry(record):
        raise PermissionError(
            "Proposal has expired"
        )

    current = get_automation_config(
        record["automation_id"]
    )


    if config_fingerprint(current) != record[
        "original_fingerprint"
    ]:
        raise RuntimeError(
            "Live configuration has changed since preparation"
        )

    result = native_validate(proposed)

    return {
        "change_id": change_id,
        "automation_id": record["automation_id"],
        "native_validation_passed": True,
        "validation_result": result,
        "conflict_detected": False,
        "approval_reference": approval_reference(
            record
        ),
        "approval_expires_at": approval_expiry(
            record
        ).isoformat(),
        "approval_required": True,
        "live_configuration_modified": False,
    }


@mcp.tool()
def apply_automation_change(
    change_id: str,
) -> dict:
    """
    Apply an independently approved test automation change.

    Supports existing automations.

    Requires matching approval in Home Assistant app
    configuration. Approval is single-use and expires
    60 minutes after preparation.

    Performs native validation, conflict detection,
    saving and read-back verification.

    Never automatically retries a failed save.
    """

    with EDIT_LOCK:
        record = load_change(change_id)

        automation_id = record["automation_id"]


        proposed = record["proposed_config"]

        check_approval(record)

        validation = native_validate(proposed)

        current = get_automation_config(
            automation_id
        )


        if config_fingerprint(current) != record[
            "original_fingerprint"
        ]:
            raise RuntimeError(
                "Configuration conflict. Nothing saved."
            )

        check_approval(record)
        consume_approval(record)

        try:
            save_response = save_native_config(
                automation_id,
                proposed,
            )

        except Exception as exc:
            return {
                "status": "save_outcome_uncertain",
                "change_id": change_id,
                "approval_consumed": True,
                "error": str(exc),
                "warning": (
                    "Inspect live configuration. "
                    "Do not automatically retry."
                ),
            }

        try:
            actual = get_automation_config(
                automation_id
            )

            actual_hash = config_fingerprint(
                actual
            )

        except Exception as exc:
            return {
                "status": "saved_but_unverified",
                "change_id": change_id,
                "approval_consumed": True,
                "save_response": save_response,
                "error": str(exc),
            }

        verified = (
            actual_hash
            == record["proposed_fingerprint"]
        )

        return {
            "status": (
                "saved_and_verified"
                if verified
                else "verification_mismatch"
            ),
            "change_id": change_id,
            "automation_id": automation_id,
            "approval_consumed": True,
            "native_validation_passed": True,
            "validation_result": validation,
            "save_response": save_response,
            "expected_fingerprint": record[
                "proposed_fingerprint"
            ],
            "actual_fingerprint": actual_hash,
            "verification_passed": verified,
            "original_backup_retained": True,
            "actual_config": actual,
        }


@mcp.tool()
def prepare_automation_rollback(
    change_id: str,
) -> dict:
    """
    Prepare restoration of an original configuration.

    Does not apply the rollback.

    Returns a new proposal requiring native validation
    and independent single-use approval.
    """

    with EDIT_LOCK:
        previous = load_change(change_id)

        automation_id = previous["automation_id"]


        current = get_automation_config(
            automation_id
        )


        original = validate_proposed_config(
            automation_id,
            previous["original_config"],
        )


        current_hash = config_fingerprint(
            current
        )

        original_hash = config_fingerprint(
            original
        )

        if current_hash == original_hash:
            return {
                "status": "already_restored",
                "live_configuration_modified": False,
            }

        if current_hash != previous[
            "proposed_fingerprint"
        ]:
            raise RuntimeError(
                "Live configuration differs from the "
                "previous proposal. Rollback refused."
            )

        record = create_change_record(
            automation_id,
            current,
            original,
            "Restore original configuration from "
            + change_id,
        )

        store_change(record)

        return {
            "status": "rollback_prepared",
            "change_id": record["change_id"],
            "automation_id": automation_id,
            "approval_required": True,
            "backup_stored": True,
            "live_configuration_modified": False,
            "diff": record["diff"],
        }


@mcp.tool()
def list_native_dashboards() -> list:
    """List dashboards using the native Lovelace API."""
    return websocket_request({"type": "lovelace/dashboards/list"})

@mcp.tool()
def list_native_helpers(domain: str) -> list:
    """List storage-managed helpers in a supported domain."""
    if domain not in HELPER_DOMAINS:
        raise ValueError("Unsupported helper domain: " + domain)
    return websocket_request({"type": domain + "/list"})

@mcp.tool()
def get_managed_config(resource: str, target: str) -> dict:
    """Read a storage dashboard by URL path (empty for default), or helper by domain/id."""
    if resource == "dashboard":
        request = {"type": "lovelace/config", "force": True}
        if target:
            request["url_path"] = target
        result = websocket_request(request)
    elif resource == "helper":
        domain, sep, item_id = target.partition("/")
        if not sep or not item_id:
            raise ValueError("Use domain/id")
        matches = [x for x in list_native_helpers(domain) if x.get("id") == item_id]
        if len(matches) != 1:
            raise ValueError("Helper not found or ambiguous")
        result = matches[0]
    else:
        raise ValueError("Use dashboard or helper")
    if not isinstance(result, dict):
        raise ValueError("No editable configuration returned")
    return result

@mcp.tool()
def prepare_managed_change(resource: str, target: str, proposed_config: dict, reason: str) -> dict:
    """Prepare replacement of an existing storage dashboard or helper. No live writes."""
    if not reason.strip() or len(reason) > 2000:
        raise ValueError("Provide a reason")
    original = get_managed_config(resource, target)
    if len(canonical_config(proposed_config)) > MAX_CONFIG_BYTES:
        raise ValueError("Configuration too large")
    if resource == "dashboard":
        if not isinstance(proposed_config.get("views"), list):
            raise ValueError("Dashboard must contain a views list")
    elif resource == "helper":
        if proposed_config.get("id") != original.get("id") or set(proposed_config) != set(original):
            raise ValueError("Preserve helper ID and existing field set")
    if original == proposed_config:
        return {"status": "no_changes"}
    record = create_change_record(target, original, proposed_config, reason)
    record["resource"] = resource
    store_change(record)
    return {"change_id": record["change_id"], "resource": resource, "target": target,
            "diff": record["diff"], "approval_reference": approval_reference(record),
            "approval_expires_at": approval_expiry(record).isoformat(),
            "native_validation_performed": False, "live_configuration_modified": False}

@mcp.tool()
def review_managed_change(resource: str, change_id: str) -> dict:
    """Review a dashboard or helper proposal and current configuration."""
    if resource not in {"dashboard", "helper"}:
        raise ValueError("Unsupported resource")
    r = load_change(change_id, resource)
    current = get_managed_config(resource, r["automation_id"])
    return {**r, "approval_consumed": approval_used(r),
            "matches_original": config_fingerprint(current) == r["original_fingerprint"],
            "matches_proposal": config_fingerprint(current) == r["proposed_fingerprint"]}

@mcp.tool()
def apply_managed_change(resource: str, change_id: str) -> dict:
    """Apply approved storage dashboard/helper edit. Native save validates supported fields."""
    if resource not in {"dashboard", "helper"}:
        raise ValueError("Unsupported resource")
    with EDIT_LOCK:
        r = load_change(change_id, resource)
        check_approval(r)
        target = r["automation_id"]
        proposed = r["proposed_config"]
        current = get_managed_config(resource, target)
        if config_fingerprint(current) != r["original_fingerprint"]:
            raise RuntimeError("Configuration conflict")
        if resource == "dashboard":
            request = {"type": "lovelace/config/save", "config": proposed}
            if target:
                request["url_path"] = target
        else:
            domain, _, item_id = target.partition("/")
            if domain not in HELPER_DOMAINS or proposed.get("id") != item_id:
                raise ValueError("Invalid helper target")
            request = {"type": domain + "/update", domain + "_id": item_id}
            request.update({k: v for k, v in proposed.items() if k != "id" and current.get(k) != v})
        check_approval(r)
        consume_approval(r)
        try:
            websocket_request(request)
            actual = get_managed_config(resource, target)
        except Exception as exc:
            return {"status": "save_outcome_uncertain", "approval_consumed": True, "error": str(exc)}
        verified = config_fingerprint(actual) == r["proposed_fingerprint"]
        return {"status": "saved_and_verified" if verified else "verification_mismatch", "actual_config": actual, "approval_consumed": True}

@mcp.tool()
def prepare_managed_rollback(resource: str, change_id: str) -> dict:
    """Prepare separately approved restoration of a dashboard or helper."""
    if resource not in {"dashboard", "helper"}:
        raise ValueError("Unsupported resource")
    r = load_change(change_id, resource)
    current = get_managed_config(resource, r["automation_id"])
    if config_fingerprint(current) != r["proposed_fingerprint"]:
        raise RuntimeError("Rollback conflict")
    return prepare_managed_change(resource, r["automation_id"], r["original_config"], "Restore " + change_id)


class TokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request,
        call_next,
    ):
        authorization = request.headers.get(
            "authorization",
            "",
        )

        expected = f"Bearer {TOKEN}"

        if not secrets.compare_digest(
            authorization,
            expected,
        ):
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
            )

        return await call_next(request)


app = mcp.streamable_http_app()
app.add_middleware(TokenMiddleware)
