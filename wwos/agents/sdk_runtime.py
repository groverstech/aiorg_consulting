"""Claude Agent SDK runtime (``WWOS_AGENT_RUNTIME=agent_sdk``).

The Agent SDK drives the agent loop (it runs the Claude Code CLI as a subprocess); every safeguard of
the Messages runtime is preserved:

* **No built-in tools** (``tools=[]``): no shell, file, web or sub-agent tools exist in the session.
* **Only gated business tools**: each tool the agent holds is exposed as an in-process MCP tool
  whose handler calls ``runtime.execute_tool`` (schema validation → permission → owner approval →
  handler → audit). Tools the agent does not hold are not exposed at all.
* **Fail-closed PreToolUse hook** denies any tool outside that set (``on_failure="block"``), and
  ``permission_mode="dontAsk"`` denies anything that would otherwise prompt.
* **Per-call budget reservation** through ``budget_proxy`` (the CLI's ``ANTHROPIC_BASE_URL``).
* **Isolation**: ``setting_sources=[]``, ``strict_mcp_config``, auto-memory and prompt history off,
  a private empty working directory and config directory per run (deleted afterwards), secrets and
  proxy variables blanked in the subprocess environment, the real API key never passed to it.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import budget_guard
from ..budget import BudgetExceeded
from ..llm import PermanentLLMError, TransientLLMError
from ..models import Agent, Tenant
from ..tools import TOOLS, ToolContext
from . import budget_proxy

SERVER = "wwos"
_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "DATABASE_URL", "AUTH")
_PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")


def available() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False
    return True


def sdk_tool_name(name: str) -> str:
    """``crm.upsert_contact`` -> ``crm_upsert_contact`` (MCP names are later prefixed mcp__wwos__)."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


# Variables the CLI may see. Everything else in the parent environment is dropped by the launcher
# (an ALLOWLIST: the Python SDK merges options.env over the inherited environment, and the CLI reads
# many inherited CLAUDE_* variables, some of which it injects into prompts).
_ALLOWED_ENV = {
    "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "NO_PROXY", "no_proxy", "CLAUDE_CONFIG_DIR", "HOME",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "CLAUDE_CODE_SKIP_PROMPT_HISTORY", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "ENABLE_CLAUDEAI_MCP_SERVERS", "DISABLE_TELEMETRY", "DISABLE_AUTOUPDATER", "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "PATH", "LANG", "LC_ALL", "TZ", "TMPDIR",
}
_SDK_HANDSHAKE_PREFIXES = ("CLAUDE_AGENT_SDK", "CLAUDE_CODE_ENTRYPOINT")
_launcher_path: str | None = None


def _launcher() -> str:
    """Write (once per process) an executable that execs the bundled CLI with an allowlisted environment."""
    global _launcher_path
    if _launcher_path and os.path.exists(_launcher_path):
        return _launcher_path
    import stat
    import sys
    from pathlib import Path

    import claude_agent_sdk

    real = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    if not real.exists():
        raise RuntimeError("bundled Claude Code CLI not found in claude_agent_sdk")
    d = tempfile.mkdtemp(prefix="wwos-cli-")
    path = os.path.join(d, "claude")
    with open(path, "w") as f:
        f.write(f"""#!{sys.executable} -I
import os, sys
if sys.platform.startswith("linux"):
    # Parent-death signal: if the worker process dies (even SIGKILL), the kernel kills the CLI too,
    # so no orphaned agent process keeps running. Survives the execve below.
    import ctypes, signal
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, int(signal.SIGKILL), 0, 0, 0)
    if os.getppid() == 1:  # parent already gone between fork and prctl
        sys.exit(1)
allowed = set({sorted(_ALLOWED_ENV)!r})
env = {{k: v for k, v in os.environ.items() if k in allowed or k.startswith({_SDK_HANDSHAKE_PREFIXES!r})}}
os.execve({str(real)!r}, [{str(real)!r}] + sys.argv[1:], env)
""")
    os.chmod(path, stat.S_IRWXU)
    import atexit

    atexit.register(shutil.rmtree, d, True)
    _launcher_path = path
    return path


def subprocess_env(base_url: str, config_dir: str, max_output_tokens: int) -> dict[str, str]:
    """Environment for the CLI. Defence in depth: secret-looking and proxy variables are blanked here,
    and the launcher (cli_path) additionally drops everything not on the allowlist."""
    env = {k: "" for k in os.environ if any(h in k.upper() for h in _SECRET_HINTS)}
    env.update({k: "" for k in _PROXY_VARS})
    env.update({
        "HOME": config_dir,
        "TMPDIR": config_dir,  # CLI scratch files land in the per-run directory that is deleted afterwards
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_API_KEY": "wwos-proxy-holds-the-real-key",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "CLAUDE_CONFIG_DIR": config_dir,
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_output_tokens),
    })
    return env


def _run_blocking(coro_factory) -> None:
    """Run an async SDK session to completion from synchronous code. Nested runs happen inside a tool
    handler of an outer session (e.g. a new agent's acceptance test during agents.provision), where an
    event loop is already running in this thread: run those on a dedicated thread and wait. The outer
    session is blocked awaiting the tool result, so the shared DB session is never used concurrently."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro_factory())
        return
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="wwos-nested-agent") as ex:
        ex.submit(asyncio.run, coro_factory()).result()


def run_agent_prompt_sdk(s: Session, tenant: Tenant, agent: Agent, prompt: str, *, max_tokens: int,
                         allow_tools: bool, task_id: str | None, trace, depth: int, system: str,
                         max_turns: int) -> str:
    from claude_agent_sdk import (ClaudeAgentOptions, HookMatcher, ResultMessage, create_sdk_mcp_server,
                                  query, tool)

    from .runtime import SpendRecord, execute_tool

    ctx = ToolContext(session=s, tenant=tenant, agent=agent, task_id=task_id, trace=trace, depth=depth)
    offered = [t for t in (agent.tools or []) if t in TOOLS] if allow_tools else []
    names = {sdk_tool_name(t): t for t in offered}
    allowed = {f"mcp__{SERVER}__{n}" for n in names}

    def make(sdk_name: str, wwos_name: str):
        @tool(sdk_name, TOOLS[wwos_name].description, TOOLS[wwos_name].schema)
        async def handler(args):
            out, is_err = execute_tool(ctx, wwos_name, dict(args or {}), trace)
            trace.tool_calls.append({"tool": wwos_name, "error": is_err})
            return {"content": [{"type": "text", "text": json.dumps(out, default=str)}], "is_error": is_err}
        return handler

    async def guard(input_data, tool_use_id, context):
        if input_data.get("tool_name") in allowed:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": "only WWOS-gated tools may be used"}}

    lineage = [i for (i,) in s.execute(select(Agent.id).where(Agent.lineage_id == agent.lineage_id))]
    run = budget_proxy.RunBudget(tenant_id=tenant.id, agent_id=agent.id, agent_name=agent.name,
                                 agent_budget=Decimal(agent.monthly_budget_usd), lineage_ids=lineage,
                                 task_id=task_id, model=agent.model, max_output_tokens=max_tokens)
    token = budget_proxy.register(run)
    workdir = tempfile.mkdtemp(prefix="wwos-agent-")
    config_dir = tempfile.mkdtemp(prefix="wwos-agent-cfg-")
    options = ClaudeAgentOptions(
        tools=[],
        mcp_servers={SERVER: create_sdk_mcp_server(SERVER, tools=[make(n, w) for n, w in names.items()])},
        strict_mcp_config=True,
        allowed_tools=sorted(allowed),
        permission_mode="dontAsk",
        setting_sources=[],
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[guard], on_failure="block")]},
        system_prompt=system,
        model=agent.model,
        max_turns=max_turns,
        cwd=workdir,
        env=subprocess_env(budget_proxy.base_url(token), config_dir, max_tokens),
        cli_path=_launcher(),
    )
    result = None

    async def drive():
        nonlocal result
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                result = message

    error: Exception | None = None
    try:
        _run_blocking(drive)
    except Exception as e:  # noqa: BLE001 - classified below
        error = e
    finally:
        finished = budget_proxy.unregister(token) or run
        for rid in finished.reservations:
            budget_guard.track(s, rid)  # ledger written when the session commits or rolls back
        for model, inp, out, rid in finished.spend:
            trace.spend.append(SpendRecord(agent.id, task_id, model, inp, out, reservation_id=rid))
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(config_dir, ignore_errors=True)
    if run.refused:
        raise BudgetExceeded(run.refused)
    if run.upstream_status in (429, 500, 502, 503, 529):
        raise TransientLLMError(f"model API unavailable (HTTP {run.upstream_status})")
    if error is not None:
        raise PermanentLLMError(f"agent SDK run failed: {error}") from error
    if result is None or result.subtype != "success":
        raise PermanentLLMError(f"agent SDK run ended with {getattr(result, 'subtype', 'no result')}")
    return result.result or ""
