import asyncio
import json
import logging
import shlex
from typing import Any

from app.config import settings
from app.exceptions import LarkCLIError

logger = logging.getLogger(__name__)


async def run_lark(
    *args: str,
    as_identity: str | None = None,
    timeout: int | None = None,
    input_data: str | None = None,
    no_format: bool = False,
) -> Any:
    """
    Execute a lark-cli command and return parsed JSON output.

    Usage:
        result = await run_lark("calendar", "events", "instance_view",
                                "--params", json.dumps({...}))
        result = await run_lark("im", "+messages-send",
                                "--user-id", "ou_xxx",
                                "--content", json.dumps(card))
    """
    identity = as_identity or settings.LARK_CLI_AS
    cmd = [settings.LARK_CLI_PATH, *args, "--as", identity]
    if not no_format:
        cmd += ["--format", "json"]
    timeout_s = timeout or settings.LARK_CLI_TIMEOUT

    logger.debug("lark-cli: %s", shlex.join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if input_data else None,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input_data.encode() if input_data else None),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise LarkCLIError(f"lark-cli timed out after {timeout_s}s: {shlex.join(cmd)}")

    if proc.returncode != 0:
        err = stderr.decode().strip()
        raise LarkCLIError(
            f"lark-cli exit {proc.returncode}: {err}",
            cmd=list(cmd),
            stderr=err,
        )

    raw = stdout.decode().strip()
    if not raw:
        return {}

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("lark-cli non-JSON output: %s", raw[:200])
        return {"raw": raw}


async def stream_lark_events(
    *args: str,
    as_identity: str = "bot",
    force: bool = False,
) -> asyncio.subprocess.Process:
    """
    Start a long-running lark-cli event +subscribe process.
    Returns the process; caller reads stdout line-by-line.
    force=True adds --force to evict any existing subscriber for this app.
    """
    cmd = [settings.LARK_CLI_PATH, "event", "+subscribe", "--as", as_identity, *args]
    if force:
        cmd.append("--force")
    logger.info("Starting lark event stream: %s", shlex.join(cmd))
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
