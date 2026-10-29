# BRIEFING — 2026-10-29T12:30:00Z

## Mission
Investigate Retool AI relay architecture, server entry points, proxy endpoints, upstream Retool requests, streaming/buffering, headers/payloads, and serialization/network latency.

## 🔒 My Identity
- Archetype: explorer
- Roles: relay architecture explorer, investigation
- Working directory: c:\specialinay_papka\.agents\teamwork\teamwork_explorer_survey_1
- Original parent: 24ba9642-7870-46ab-ac84-4f9c9c07479d
- Milestone: survey phase

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- Full context preservation constraint: no artificial truncation or capping
- Maintain effortLevel: high
- Verification on preserved Claude CLI sessions later by implementer/tester

## Current Parent
- Conversation ID: 24ba9642-7870-46ab-ac84-4f9c9c07479d
- Updated: 2026-10-29T12:30:00Z

## Investigation State
- **Explored paths**: `proxy.py`, `orchestrator.py`, `direct.py`, `session.py`, `validate.py`, `context.py`, `adapters.py`, `relay.log`, Claude CLI sessions in `C:\Users\Redmi\.claude\projects\C--specialinay-papka\`.
- **Key findings**:
  1. Server operates on 127.0.0.1:8787 via `BoundedServer` (ThreadingHTTPServer) with `SO_EXCLUSIVEADDRUSE` on NT.
  2. Relay uses two upstream transports: legacy WebSocket agent transport (`/api/ai/retool-next-agent/start`) and direct copilot transport (`/api/gpt/copilotStreamGPT?origin=frontend`).
  3. Direct transport supports prompt caching via `openAICacheKey` and `block["cache"] = True`, with keep-warm background loop.
  4. Root causes of 40-second turnaround stalls identified:
     - Complete cache invalidation when dynamic tool sets change (e.g. 15 tools -> 18 tools causes full 158k-token prefill stall of 17-38s).
     - Tool-less requests (such as title queries, `/compact` summarization, `/goal`) trigger `isolated=True` and inject 30,000 bytes of relay project context (`build_system_instructions()`), changing `head` and invalidating `openAICacheKey`, resulting in full cold re-reads.
     - `_CACHE_MIN_CHARS = 3000` completely shuts off prompt caching (`openAICacheKey` and system `cache: True` not sent) whenever `len(head) < 3000`.
     - Direct transport flattens all conversation history into `role: "user"` text strings, which prevents proper prefix alignment when client formats vary.
     - Subagent invocations change declared tools, breaking the parent's cached prefix.
     - Upstream `claude-opus-5-5` with `effort: high` takes significant inference time; when compounded with uncached prefill of 158k tokens, turns reach 38-45 seconds.
- **Unexplored areas**: Code modification and live test validation (reserved for implementer/tester roles).

## Key Decisions Made
- Fully documented evidence chains (file paths, line numbers, verbatim logs).
- Authored comprehensive `analysis.md` and `handoff.md`.

## Artifact Index
- `c:\specialinay_papka\.agents\teamwork\teamwork_explorer_survey_1\DISPATCH.md` — Inbound instructions
- `c:\specialinay_papka\.agents\teamwork\teamwork_explorer_survey_1\BRIEFING.md` — Situational awareness
- `c:\specialinay_papka\.agents\teamwork\teamwork_explorer_survey_1\progress.md` — Liveness & status tracking
- `c:\specialinay_papka\.agents\teamwork\teamwork_explorer_survey_1\analysis.md` — In-depth architectural survey report
- `c:\specialinay_papka\.agents\teamwork\teamwork_explorer_survey_1\handoff.md` — 5-component handoff report
