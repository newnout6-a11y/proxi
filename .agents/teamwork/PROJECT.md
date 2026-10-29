# Project: Retool AI Relay Latency Elimination & Prompt Cache Optimization

## Architecture
- Local Retool AI Relay Server running on `127.0.0.1:8787`.
- Primary components:
  - `proxy.py`: HTTP entrypoint, routing, request parsing, session handling.
  - `direct.py`: Direct copilot transport (`/api/gpt/copilotStreamGPT`), prompt caching via `openAICacheKey` and `cache: True`, keepwarm loop.
  - `orchestrator.py`: Multi-model orchestration, fallback, subagent routing, tool schema management.
  - `context.py`: Dialogue history reconstruction, context preservation, prefix alignment.
  - `session.py`: Session persistence, Claude CLI session mapping.
  - `adapters.py`: Format translation between Anthropic Messages API and Retool Copilot API.
  - `validate.py`: Request validation, model parameter enforcement (effort level clamping/preservation).

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Stable Cache Prefix & Tool Hash Normalization | Prevent tool set additions/variations or subagent tool declarations from invalidating the prompt cache prefix. | M1 | Survey (Explorer 1 & 2) |
| 2 | Isolated Request & Tool-less History Alignment | Stop tool-less requests (/compact, /goal, title generation) from injecting 30k divergent context that invalidates `openAICacheKey`. | M1 | Survey (Explorer 1) |
| 3 | Remove / Fix `_CACHE_MIN_CHARS` Restriction | Ensure prompt caching operates reliably on short heads / prefixes without arbitrary character thresholds. | M1 | Survey (Explorer 1) |
| 4 | Effort Level Clamping & Error Prevention | Ensure `effortLevel: high` is preserved for primary reasoning models while preventing invalid `effort: xhigh` 400 errors on Haiku during /goal. | M1 | Survey (Explorer 3) |
| 5 | Full Context Preservation | Maintain full conversation history without artificial truncation, 100k limits, or message dropping. | M1 | Survey (R2 constraint) |
| 6 | Verification on Claude CLI Session `e6b8a126...` | Verify consecutive turns, /compact, /goal, subagents, measure cache hits in relay.log. | M2 | Survey (R3 constraint) |
| 7 | Verification on Claude CLI Session `bd9930ab...` | Verify consecutive turns, /compact, /goal, subagents on second session without collision. | M2 | Survey (R3 constraint) |
| 8 | Regression & Forensic Audit | Run pytest test suite (652+ tests), verify zero integrity violations or artificial caps. | M3 | Audit & Quality |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Relay Cache & Turnaround Optimization | Fix tool prefix invalidation, isolated context divergence, `_CACHE_MIN_CHARS`, effort level handling in `direct.py`, `context.py`, `adapters.py`, `proxy.py`. | none | DONE |
| M2 | Dual-Session Claude CLI Live Verification | Execute consecutive turns, `/compact`, `/goal`, and subagents on `e6b8a126...` and `bd9930ab...`, verify cache hits & sub-5s response times in `relay.log`. | M1 | DONE |
| M3 | Comprehensive Regression Test & Forensic Audit | Full pytest suite run, verify zero context truncation/capping, verify effortLevel: high maintained, forensic audit check. | M2 | DONE |

## Code Layout
- `direct.py`: Direct copilot transport, `openAICacheKey`, `_CACHE_MIN_CHARS`, prompt caching blocks.
- `context.py`: History assembly, context injection, prefix stability.
- `adapters.py`: Payload serialization, tool definition hashing, message formatting.
- `proxy.py`: Request dispatching, isolated request handling.
- `tests/`: Existing test suite (652 tests).
- `relay.log`: Upstream request and cache hit/miss logs.
