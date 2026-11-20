"""Run read-only MCP internally; no ports exposed by the Home Assistant add-on."""
import os
import secrets
from pathlib import Path

secret_file = Path('/data/mcp_token')
if not secret_file.exists():
    secret_file.write_text(secrets.token_urlsafe(48), encoding='utf-8')
    secret_file.chmod(0o600)
os.environ['HA_YAML_MCP_TOKEN'] = secret_file.read_text(encoding='utf-8').strip()
os.environ['HA_CONFIG_DIR'] = '/homeassistant'
os.environ['HA_YAML_MCP_BIND'] = '0.0.0.0'
os.environ['HA_YAML_MCP_PORT'] = '8765'
from ha_yaml_mcp.server import app
import uvicorn
uvicorn.run(app, host='0.0.0.0', port=8765)
