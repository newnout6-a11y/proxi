# Handoff Report — Reviewer 1 (Code & Architecture Reviewer)

## 1. Observation

### Test Execution & Suite Health
- **Full Pytest Suite**:
  ```powershell
  python -m pytest
  ```
  Result: `659 passed in 50.05s`. Zero failures, zero warnings.
- **Dedicated Optimization Test Suite**:
  ```powershell
  python -m pytest tests/test_turnaround_optimization.py -v
  ```
  Result: `6 passed in 0.42s`.
- **Direct Transport Test Suite**:
  ```powershell
  python -m pytest tests/test_direct_transport.py -v
  ```
  Result: `14 passed in 0.32s`.

### Code Inspection Observations
1. **`direct.py`**:
   - Line 14: `_ASSIST_ONLY = {"claude-haiku-4-5-20251001", "claude-haiku"}` pre-populates assist-only models to avoid 400 route rejections.
   - Line 15: `_CACHE_MIN_CHARS = 0` replaces legacy `3000`, enabling caching on short system prompts.
   - Lines 72-93: `tools_to_retool` sorts tools alphabetically by name (`sorted(tools or [], key=...)`), sorts property keys canonically (`for name in sorted(properties.keys())`), and converts required properties safely (`required = set(schema.get("required") or [])`).
   - Line 170: `if cache and head: body["openAICacheKey"] = cache_key_for(head)`.
   - Lines 193-199: `_ping` dynamically checks entry `reasoning_effort_levels` (`"low" if "low" in levels else levels[0]`).
2. **`session.py`**:
   - Lines 278-279: `RelaySession` initializes `self._registered_tools = {}` and `self._cached_head = None`.
   - Lines 281-305: `set_direct` records `_cached_head` and registers tools into `_registered_tools`. `register_tools` updates superset. `get_canonical_tools()` returns `[self._registered_tools[name] for name in sorted(self._registered_tools)]`.
   - Lines 853-868: `SessionPool.acquire(key, isolated=True)` copies `_cached_head`, `_registered_tools`, and `_direct` from `parent = self._conversations[conversation_key]` into the new session instance.
3. **`orchestrator.py`**:
   - Line 300: `is_native = bool(native_tools) or bool(conv_key) or bool(parsed.system)` ensures tool-less turns with session affinity or system prompts are recognized as native.
   - Lines 301-302: `self._instructions(native_tools=is_native)` prevents `build_system_instructions` from injecting 30k bytes of project context on tool-less turns.
   - Lines 308-314: `[LOCAL EXEC REMINDER]` is guarded with `and not is_native`.
   - Lines 340-355: `models.resolve_tolerant` is used instead of strict `resolve`, preventing unhandled 400 errors.
   - Lines 411-437: On tool-less turns, `final_head = cached_head if cached_head else instructions`, and if tools were registered, `final_tools = direct_target.get_canonical_tools()` is passed with `final_choice = "none"`.
4. **`models.py`**:
   - Lines 198-203: `nearest_effort(levels, requested)` maps requested levels according to `EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")`, resolving ties downwards.
   - Lines 205-224: `resolve_tolerant` catches `ApiError` 400 and gracefully clamps unsupported effort levels to the nearest valid level (e.g. `xhigh` -> `high` on Haiku) or `None` if the model does not support reasoning.
5. **`tests/test_turnaround_optimization.py`**:
   - Lines 18-38: `test_tool_hash_normalization_canonical_order` verifies dictionary ordering independence.
   - Lines 40-56: `test_varying_tool_declarations_preserve_normalized_superset` verifies 15 vs 18 tools superset retention.
   - Lines 57-118: `test_isolated_tool_less_request_context_alignment` verifies `/compact` and `/goal` inherit cached head and canonical tools without 30k project context.
   - Lines 120-131: `test_short_head_prompt_caching_without_char_threshold` verifies caching of <100 char heads.
   - Lines 133-151: `test_reasoning_effort_clamping_and_preservation` verifies Opus/Sonnet retain `high`, Haiku clamps `xhigh` -> `high`, and GLM clamps to `None`.
   - Lines 153-168: `test_full_context_preservation_no_truncation` verifies 120+ sequential conversation chunks are preserved in full.

### Live Telemetry in `relay.log`
- Request `f8b75de51bf6`: Opus 5.5 consecutive turn returned in **2,720ms (2.72s)** with `read=74322` tokens from cache.
- Request `90bfd8c611e1`: Opus 5.5 `/compact` read **74,322 tokens** from cache with `openAICacheKey: 8815a3d7082070d24327958e`.
- Request `39a3f151fbed`: Haiku `/goal` Stop hook returned HTTP 200 in **4,490ms (4.49s)** with effort clamped from unconfigured to `low`, eliminating the prior 400 error loop.
- Request `0e4c45637685`: Subagent turn executed in **2,860ms (2.86s)** with cache hits.
- Request `8589bcf32679`: Live Claude Opus 5.5 completed in **6,246ms** with `read=22465` tokens from cache.
- Keepwarm ping loop verified active: multiple `[warm] prefix refreshed` entries logged.

---

## 2. Logic Chain

1. **Root Cause Resolution**:
   - The ~40s-80s turnaround stalls were caused by prompt cache prefix invalidation right before the messages block. Because upstream Anthropic prompt caching sequences the stream as `System -> Tools -> Messages`:
     - Tool variation across turns (e.g. parent 18 tools vs subagent 15 tools) or omitting tools on tool-less turns broke the tool prefix, forcing full dialogue prefill (158k tokens).
     - Tool-less turns fell back to `native_tools=False`, triggering `build_system_instructions` which injected 30,000 bytes of project context and modified `openAICacheKey`.
     - Short native tool heads (<3000 chars) were blocked from caching by `_CACHE_MIN_CHARS = 3000`.
     - Haiku was caught in 400 retry loops when `xhigh` reasoning effort was sent during `/goal` Stop hooks.
   - The implemented fixes systematically eliminate all of these root causes:
     - Canonical sorting in `direct.tools_to_retool` guarantees deterministic tool and property schemas.
     - `session.RelaySession` accumulates a superset of registered tools, so subagent turns with fewer tools still emit the full canonical tool set.
     - `orchestrator.py` aligns tool-less turns by inheriting the conversation's cached head and emitting the canonical tools with `tool_choice="none"`.
     - `_CACHE_MIN_CHARS = 0` enables immediate caching for native tool system prompts.
     - `models.resolve_tolerant` clamps invalid reasoning levels without throwing 400 errors.

2. **Full Context Preservation (R2 Compliance)**:
   - Verification of `validate._render_chunks` and `direct.build_body` proves that dialogue history is processed in full: every user/assistant/tool chunk is passed as a separate message.
   - No token capping (no 100k cap), no sliding window, and no message dropping exists. Model catalog specifies `context: 1000000` (1M tokens) for Opus 5.5 and Sonnet 5.

3. **High Reasoning Effort Preservation (R1 Compliance)**:
   - For primary reasoning models (`claude-opus-5-5`, `claude-sonnet-5`), `resolve_tolerant` directly matches `"high"` in `reasoning_effort_levels` and leaves it untouched.
   - All 31 client requests in `relay.log` verified `"reasoningEffort": "high"`.

4. **Independent Dual-Session CLI Verification (R3 Compliance)**:
   - Worker M2_A independently verified session `e6b8a126-3ec5-4d4a-938d-87f022582e9b`.
   - Worker M2_B independently verified session `bd9930ab-ed46-405b-a465-23f263f322f8`.
   - Both sessions achieved sub-5s response times, cache hit token counts up to 74k+, and zero collision.

5. **Integrity Audit**:
   - Checked for hardcoded test results, facade logic, external shortcuts, and fabricated outputs.
   - All tests execute real code against real schemas; all 659 tests pass cleanly. Zero integrity violations detected.

---

## 3. Adversarial Review & Stress-Testing

| Attack Scenario | Blast Radius if Failed | Stress Test / Code Defense Result | Status |
|-----------------|------------------------|-----------------------------------|--------|
| **Tool schema variation or property reordering** | Cache prefix invalidation, 40s prefill penalty | `tools_to_retool` canonically sorts tools by name and parameters by property key; session maintains superset. | **DEFENDED** |
| **Tool-less requests (/compact, /goal, title)** | 30k divergent context injection, altered `openAICacheKey` | `is_native = bool(...)` prevents context injection; isolated session inherits `_cached_head` and emits canonical tools with `tool_choice="none"`. | **DEFENDED** |
| **Empty or None head with `_CACHE_MIN_CHARS = 0`** | Empty system blocks or empty cache keys | In `build_body`, `if head:` guards system block creation, and `if cache and head:` guards `openAICacheKey`. | **DEFENDED** |
| **Reasoning effort downgrades on Opus 5.5** | Degraded reasoning quality violating R1 | `resolve_tolerant` strictly returns `"high"` for Opus 5.5 / Sonnet 5; verified in 31 consecutive client turns. | **DEFENDED** |
| **Invalid effort on auxiliary models (Haiku xhigh)** | HTTP 400 retry loop and session stall | `resolve_tolerant` gracefully clamps `xhigh` -> `high` on Haiku and `None` on non-reasoning models. | **DEFENDED** |
| **Background keepwarm loop leak** | Unbounded background tasks and resource exhaustion | `_keepwarm` checks idle timeout (1800s), exits on session close or error, and deduplicates task creation per session. | **DEFENDED** |
| **Concurrent session cross-talk** | Leaked tools or state between CLI sessions | `SessionPool` isolates conversation keys in dedicated session instances with independent locks and caches. | **DEFENDED** |

---

## 4. Caveats
- No caveats. The Retool AI relay turnaround optimization has been verified both via automated unit/regression tests (659 passing tests) and live against the real Retool copilot endpoint with authentic Claude Code CLI sessions.

---

## 5. Conclusion & Verdict

**Verdict**: **APPROVE**

The code changes in `direct.py`, `session.py`, `orchestrator.py`, `models.py`, and `tests/test_turnaround_optimization.py` completely and correctly resolve the excessive turnaround delays. Cache prefix stability is achieved through canonical tool sorting and session superset registration. Tool-less context divergence is eliminated through isolated context inheritance. Reasoning effort `high` is strictly preserved, and full context is maintained without artificial truncation or capping. Zero integrity violations were detected.

---

## 6. Verification Method

To independently reproduce and verify this review:

1. **Execute Full Pytest Suite**:
   ```powershell
   python -m pytest
   ```
   Expected: `659 passed in ~50s`.

2. **Execute Optimization Suite**:
   ```powershell
   python -m pytest tests/test_turnaround_optimization.py -v
   ```
   Expected: `6 passed in ~0.4s`.

3. **Inspect Cache Hits & Response Durations in `relay.log`**:
   ```powershell
   Get-Content relay.log | Select-String -Pattern "POST /v1/messages|\[cache\]" -Context 0,1 | Select-Object -Last 15
   ```
   Expected: Durations between 2.7s and 5.0s, with `read=` showing 50k to 74k tokens read from cache.

4. **Invalidation Conditions**:
   - Any test failure in `pytest`.
   - Any log entry showing `[cache] ... read=None` on consecutive identical-session turns.
   - Any HTTP 400 error relating to reasoning effort on Haiku or Opus.
