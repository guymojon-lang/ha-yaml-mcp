"""Read-only MCP prototype for allowlisted Home Assistant YAML files."""
import os
import secrets
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount
import uvicorn

CONFIG = Path(os.environ.get('HA_CONFIG_DIR', '/config')).resolve()
ALLOWED = frozenset({
    'configuration.yaml', 'automations.yaml', 'scripts.yaml',
    'scenes.yaml', 'groups.yaml', 'customize.yaml', 'templates.yaml',
})
TOKEN = os.environ.get('HA_YAML_MCP_TOKEN', '')
if len(TOKEN) < 32:
    raise RuntimeError('HA_YAML_MCP_TOKEN must be at least 32 characters')

mcp = FastMCP('Home Assistant YAML (read-only)', stateless_http=True, json_response=True)


def allowed_path(filename: str) -> Path:
    if filename not in ALLOWED:
        raise ValueError('File is not on the read-only allowlist')
    p = CONFIG / filename
    if p.is_symlink() or p.resolve().parent != CONFIG:
        raise ValueError('Unsafe path')
    if not p.is_file():
        raise FileNotFoundError(filename)
    return p


@mcp.tool()
def list_yaml_files() -> list[str]:
    """List allowlisted YAML files present in the Home Assistant config folder."""
    return sorted(name for name in ALLOWED if (CONFIG / name).is_file() and not (CONFIG / name).is_symlink())


@mcp.tool()
def read_yaml_file(filename: str) -> str:
    """Read an allowlisted YAML file. Does not modify files."""
    p = allowed_path(filename)
    if p.stat().st_size > 1024 * 1024:
        raise ValueError('File too large for this prototype')
    return p.read_text(encoding='utf-8')


class TokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        authorization = request.headers.get('authorization', '')
        expected = f'Bearer {TOKEN}'
        if not secrets.compare_digest(authorization, expected):
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        return await call_next(request)


app = Starlette(routes=[Mount('/mcp', app=mcp.streamable_http_app())])
app.add_middleware(TokenMiddleware)

if __name__ == '__main__':
    uvicorn.run(app, host=os.environ.get('HA_YAML_MCP_BIND', '127.0.0.1'), port=int(os.environ.get('HA_YAML_MCP_PORT', '8765')))
