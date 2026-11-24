"""Run the Home Assistant YAML MCP server."""

import os
import secrets
from pathlib import Path

secret_file = Path("/data/mcp_token")

if not secret_file.exists():
    secret_file.write_text(
        secrets.token_urlsafe(48),
        encoding="utf-8",
    )
    secret_file.chmod(0o600)

os.environ["HA_YAML_MCP_TOKEN"] = (
    secret_file.read_text(encoding="utf-8").strip()
)

os.environ["HA_CONFIG_DIR"] = "/homeassistant"
os.environ["HA_YAML_MCP_BIND"] = "0.0.0.0"
os.environ["HA_YAML_MCP_PORT"] = "8765"

# Supervisor API authentication diagnostics

token_file = Path(
    "/run/s6/container_environment/SUPERVISOR_TOKEN"
)

print(
    "Supervisor token in process environment:",
    bool(os.environ.get("SUPERVISOR_TOKEN")),
    flush=True,
)

print(
    "Supervisor token file exists:",
    token_file.is_file(),
    flush=True,
)

if not os.environ.get("SUPERVISOR_TOKEN"):
    if token_file.is_file():
        token = token_file.read_text(
            encoding="utf-8"
        ).strip()

        if token:
            os.environ["SUPERVISOR_TOKEN"] = token
            print(
                "Supervisor token recovered from s6 environment",
                flush=True,
            )

print(
    "Supervisor API token available:",
    bool(os.environ.get("SUPERVISOR_TOKEN")),
    flush=True,
)

from ha_yaml_mcp.server import app

import uvicorn

uvicorn.run(
    app,
    host="0.0.0.0",
    port=8765,
)
