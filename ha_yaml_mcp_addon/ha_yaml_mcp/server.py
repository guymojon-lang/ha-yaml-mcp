
import os
import secrets
from pathlib import Path

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


class TokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        authorization = request.headers.get("authorization", "")
        expected = f"Bearer {TOKEN}"
        if not secrets.compare_digest(authorization, expected):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


app = mcp.streamable_http_app()
app.add_middleware(TokenMiddleware)
