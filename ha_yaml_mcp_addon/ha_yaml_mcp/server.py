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
    """Read an existing automation through the native configuration API.

    Supply the configuration ID from automations.yaml, not the entity ID.
    This tool does not modify, reload or execute automations.
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
        raise RuntimeError(
            f"Home Assistant automation configuration API returned HTTP {exc.code}"
        ) from None
    except URLError:
        raise RuntimeError(
            "Unable to reach Home Assistant automation configuration API"
        ) from None

    if len(payload) > 1024 * 1024:
        raise ValueError("Automation configuration response too large")

    result = json.loads(payload.decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("Unexpected automation configuration response")
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
