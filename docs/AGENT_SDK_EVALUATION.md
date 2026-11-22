# Claude Agent SDK evaluation (ADR-027)

_2026-11-22. Evidence: official documentation (sources below), a hands-on integration with
`claude-agent-sdk` 0.2.187, and 12 offline tests that drive the real bundled Claude Code CLI against
a local fake Anthropic API (`tests/unit/test_agent_sdk_runtime.py`)._

## Question

The original architecture named the Claude Agent SDK. The system was built on the Anthropic Messages
API with its own tool loop, because the Agent SDK ships shell and file tools. This evaluation asks:
**can the Agent SDK be run with a restricted, securely controlled tool environment without weakening
any safeguard, and which runtime should production use?**

## Verified SDK facts (official docs)

| Fact | Source |
|---|---|
| The Python SDK runs the Claude Code CLI as a subprocess (binary bundled in the wheel) | agent-sdk/python, quickstart, SDK README |
| `tools=[]` removes **all** built-in tools; only custom (MCP) tools remain | agent-sdk/custom-tools |
| `allowed_tools` is **not** an allow-list (only auto-approval); `dontAsk` denies anything that would prompt | agent-sdk/permissions, agent-sdk/python |
| In-process tools via `@tool` + `create_sdk_mcp_server` (run in our Python process) | agent-sdk/custom-tools |
| PreToolUse hooks can deny; deny is final; `on_failure="block"` makes a crashing hook fail closed | agent-sdk/hooks, agent-sdk/permissions |
| `setting_sources=[]` stops loading user/project/local settings, but some things are read regardless (managed policy, `~/.claude.json`, auto-memory, claude.ai connectors); docs warn not to rely on defaults for multi-tenant isolation | agent-sdk/claude-code-features |
| The Python SDK **merges** `env` over the inherited process environment | agent-sdk/python |
| No hook before each model call; `max_budget_usd` is checked after responses and can overshoot | agent-sdk/agent-loop, agent-sdk/python |
| `ANTHROPIC_BASE_URL` routes every sampling request through a proxy you control | agent-sdk/secure-deployment |
| No documented offline test harness | agent-sdk (all pages listed) |

Pages: `https://code.claude.com/docs/en/agent-sdk/{overview,python,quickstart,permissions,hooks,custom-tools,agent-loop,sessions,subagents,claude-code-features,modifying-system-prompts,secure-deployment}`, `https://code.claude.com/docs/en/sandboxing`, `https://github.com/anthropics/claude-agent-sdk-python`.

## Observed behaviour (hands-on, not documented)

1. **The CLI injects inherited environment into prompts.** With every documented isolation option
   set, the first request carried a reminder block containing the account e-mail of the parent
   process's environment (`CLAUDE_CODE_USER_EMAIL`). Blanking known names is not enough because
   the CLI reads dozens of `CLAUDE_*` variables. **Mitigation implemented:** a launcher (`cli_path`)
   that execs the CLI with an environment built from an allowlist; a canary test proves no inherited
   value reaches the model.
2. **The CLI adds its own text to the context:** an identity sentence prepended to our system prompt
   and a mid-conversation system message describing its environment (working directory, platform).
   Neither contains tenant data after mitigation 1, but it is text we do not control.
3. **The CLI requests `max_tokens: 128000` on every call** (about $2.56 worst case per call on Opus
   5.5). **Mitigation:** the budget proxy clamps `max_tokens` to the run's cap before reserving.
4. **A hard-killed worker left the CLI running** (orphan). **Mitigation:** parent-death signal set
   by the launcher; test kills a worker with SIGKILL and asserts no CLI survives (mutation-checked).
5. **Nested runs** (a new agent's acceptance test inside the CEO's provisioning tool call) run inside
   an active event loop. **Mitigation:** the nested session runs on its own thread while the parent waits.
6. Cost of the subprocess: about 217 MiB resident per concurrent CLI, a 364 MB binary in the image,
   and about 1 s of overhead per run (two model calls against a local fake).

## Secure integration (implemented: `WWOS_AGENT_RUNTIME=agent_sdk`)

| Safeguard | Messages runtime | Agent SDK runtime |
|---|---|---|
| Tool surface | Only the agent's tools, defined by us | `tools=[]` (no built-ins); the agent's tools as in-process MCP tools; fail-closed PreToolUse hook; `dontAsk` |
| Gate per tool call | `execute_tool` (validation → permission → owner approval → handler → audit) | **the same function**, called from the MCP handler |
| Budget per model call | `budget_guard.reserve` before each call | same reservation, made by the in-process proxy before forwarding each call |
| Output cap | our `max_tokens` | proxy clamps the CLI's request |
| Credentials | key in our process | real key only in the proxy; the CLI holds a dummy |
| Environment & files | n/a | allowlisted environment; private temporary working and config directories, deleted after each run; no settings, memory or prompt history |
| Process lifetime | n/a | parent-death signal |
| Tested offline | provider double | the real CLI against a local fake API, including the full CEO scenario |

## Comparison

| Criterion | A: Agent SDK (restricted) | B: Messages API (custom loop) |
|---|---|---|
| Agent autonomy, specialist coordination | Equivalent: coordination, delegation and objectives are WWOS code above either runtime. The SDK adds automatic context compaction and prompt caching inside a run | Equivalent; no automatic compaction (runs are short, ≤ 8 turns) |
| Dynamic agent creation and versioning | Equivalent (WWOS lifecycle); SDK sub-agents deliberately disabled (they would bypass lifecycle controls) | Equivalent |
| Tool permissions and isolation | Equivalent after mitigations; extra layers (CLI permission engine, subprocess). **Larger attack surface**: a general-purpose agent binary whose defaults favour local coding use | Smallest surface: no tool exists unless we define it |
| Budget enforcement | Equivalent per call **only with our proxy**; the SDK's own `max_budget_usd` is not sufficient | Native |
| Approval gates, privilege escalation | Equivalent (same `execute_tool`) | Equivalent |
| Auditability and recovery | Equivalent (same audit; hooks available); CLI transcripts disabled | Equivalent |
| Multi-agent orchestration | WWOS queue and objectives (identical) | Identical |
| Scalability | ~217 MiB per concurrent run → worker needs 1 GiB (+$2.63/month at fetched prices); +364 MB image; ~1 s per run | Lightweight; 512 MiB worker |
| Maintainability | Behaviour tied to the bundled CLI version (prompt injection of context, `max_tokens` defaults, internal tools); every SDK upgrade must re-run the offline suite. Fallbacks and effort are managed by the CLI | Fewer moving parts; API features (refusal fallbacks, effort) set explicitly |
| Testability | Offline tests possible with our fake API (built here; not provided by Anthropic) | Simple provider doubles |

## Decision

1. **The Agent SDK is integrated securely and is selectable** (`WWOS_AGENT_RUNTIME=agent_sdk`). No
   shell, file, web or sub-agent capability was introduced. Every safeguard holds, demonstrated by
   12 offline tests, including the full autonomous-organisation scenario.
2. **The default production runtime remains the Messages API** (`messages`), for three reasons. It
   has the smaller attack surface (no general-purpose agent binary whose behaviour changes with CLI
   releases), needs no budget proxy to achieve per-call enforcement, and uses about a quarter of the
   memory per concurrent run. The Agent SDK's advantages (compaction, built-in caching, the Claude
   Code toolset) matter little for short business-tool runs, and the toolset is deliberately
   disabled here.
3. **Owner confirmation requested (OA-8):** keep `messages` as the default, or switch production to
   `agent_sdk`. Switching requires the image built with `--build-arg EXTRAS=postgres,agent-sdk`
   and the worker memory raised to 1 GiB, and should follow a live-model trial (OA-2).
