# Night Plan Completion Report (2026-11-24)

## 1. Integrated Local `main` Commits (`113e43e..HEAD`)

All changes were verified offline and live on isolated port `8788` and landed onto local `main` (without `git push`):

| Commit | Agent | Summary |
|---|---|---|
| `907567b` | `[fix-prompts-ui]` | Prevent 1-model prompt leaks in UI, fix LivePreview, CodexBase, and `drop_tag` |
| `3dfeecd` | `[fix-prompts-core]` | Enforce `publish_as_model`, fix pin/memo/compaction/stabilizer isolation |
| `d29b791` | `[fix-drivers-transport]` | Fix HTTP/2 sibling abort, Minara 600s hang, failover error classification, and OpenRouter `reasoning_details` |
| `31ab69f` | `[fix-core-failover]` | Fix watchdog detach, Refresher cooldown, `AccountDispatcher` races, and orchestrator accounting |
| `a282e41` | `[integrator]` | Scope `openAICacheKey` by `credential_scope` only to preserve ToolUnion and golden wire parity |
| `290db7f` | `[fix-security-config]` | Fix `cost_guard` cold-cache/save, Windows `os.replace`, secret redaction, lock cleanup, and `CreditsManager` staleness |
| `d9d1965` | `[fix-admin-journal]` | Close secret leaks, serialize live admin mutations, fix provider/client-key CRUD and journal SSE `reset` |
| `0f7b7d9` | `[fix-catalog-providers]` | Lock catalog refreshes, fix `.cn` balances, presets, Minara/Retool refresh, and Codex `context_window` |
| `213109a` | `[fix-edge-web-ui]` | Fix git allowlist case, `web_search` loop, tool-result vision, and Admin Web UI bugs |
| `1ef40c7` | `[feat-minara-responses]` | Add native Minara `/v1/responses` transport, endpoint detection, and Minara `default_reasoning_effort` |
| `6e1dcb0` | `[feat-universal-protocols]` | Complete R2 universal provider presets, reasoning params, xAI cache header, and multi-block signatures |
| `4be66e2` | `[feat-structured-logs]` | Complete R5 structured console/JSONL logging and fix proxy turn error edges |
| `8029cf1` | `[feat-ide-sync-docs]` | Harden IDE catalog sync scripts (incl. Claude Desktop `behavesAs`/`supports1m`) and update docs/`config.example.json` |
| `9c293d3` | `[integrator]` | Align `tests/test_route_alias_targets.py` with Minara `default_reasoning_effort` |
| `7380257` | `[fix-retool-4103]` | Handle Retool Cloud gate fallback and Assist 403 `plan_refusal` |

## 2. Night-Plan Scope Status

- **R1–R6 (Runtime, Protocols, Fast Models, Logs, IDE Sync)**:
  - **R1 / R2**: Completed. Universal provider presets, reasoning parameters, xAI cache header, multi-block signatures (`6e1dcb0`), native Minara `/v1/responses` transport + pre-output fallback (`1ef40c7`), and Retool `4.103.0+` Cloud gate / Assist 403 `plan_refusal` handling (`7380257`).
  - **R3 / S1**: Completed. Benchmarked Minara fast models live (see Section 3).
  - **R4**: Completed. Cross-account/credential isolation across `minara.py`, `drivers/cache_engine.py`, and `direct.py` (`credential_scope`-only `openAICacheKey` preserving ToolUnion & golden wire parity).
  - **R5**: Completed. Structured console / JSONL logging (`RELAY_LOG_FORMAT=text|structured|jsonl`, `4be66e2`), verified 100% valid JSONL with 0 secret leaks.
  - **R6**: Completed. Hardened `sync_claude_desktop_catalog.py`, `sync_harness_catalog.py`, `sync_opencode_catalog.py`, and updated `PROTOCOL.md`, `README.md`, `config.example.json` (`8029cf1`).
- **D1–D5 / S2–S4 (Security, Admin, Catalog, Edge, Web UI, Prompt Control)**:
  - Prompt Control 1-model isolation (`publish_as_model`), UI LivePreview/CodexBase/`drop_tag` (`907567b`, `3dfeecd`).
  - Transport & core failover hardening: HTTP/2 sibling abort, `<1s` silent abort, `AccountDispatcher`, `Refresher` cooldown, watchdog detach (`d29b791`, `31ab69f`).
  - Security & config: `cost_guard` cold-cache & atomic save, Windows `os.replace` retries, secret redaction, lock cleanup, `CreditsManager` staleness (`290db7f`).
  - Admin & Journal: longest-first redaction, serialized `Admin.lock` mutations, client-key CRUD, journal SSE `reset` (`d9d1965`).
  - Catalog, Edge & Web UI: catalog refresh locks, `.cn` presets, `web_search` loop, tool-result vision, Admin Web UI fixes (`0f7b7d9`, `213109a`).

## 3. Live Minara & IDE E2E Results (Isolated Port `8788`)

Tested against `9c293d3` with `RELAY_LOG_FORMAT=jsonl` across 72 active Minara accounts (41 unique paid requests, total spend **2.6534 Minara credits**, zero secret leaks):

### Protocol & Feature Matrix
- **`/v1/chat/completions` stream (`minara/deepseek-v4-flash`)**: **PASS** (`200` / `[DONE]`, TTFT `1581.9 ms`, total `1590.6 ms`, `0.0049 cr`).
- **`/v1/messages` stream (`minara/claude-sonnet-5-5`, `reasoning_effort="auto"`)**: **PASS** (`200` / `message_stop`, TTFT `2680.2 ms`, `0.1615 cr`; `effective_effort="medium"` confirmed in JSONL logs).
- **Native Minara `/v1/responses` (`minara_models.auto.json` + explicit `endpoint="responses"`)**: **UPSTREAM 404 / FALLBACK PASS**. `minara_models.auto.json` lists 0 `"openai-response"`/`"responses"` models, and live `POST https://api.minara.ai/v1/responses` returned `404 Not Found` on both `minara/gpt-6-luna-pro` and `minara/gpt-6.2-sol`. With `minara_responses_fallback=True`, the relay emitted `upstream.endpoint_fallback` (`status=404`, `path="chat"`) and completed text (`200`, `0.0624 cr`) and `function_call` (`200`, `0.0471 cr`).
- **Prompt Control 1-model isolation (`@eni`)**: **PASS** (`deepseek@eni` and `sonnet@eni` -> `404 not_found_error`; `/v1/models` exposes `minara/gpt-6.2-sol@eni` as sole `@eni` entry). Note: `/v1/messages/count_tokens` skips `_apply_prompts` and returns `200` at 0 cost.

### R3 / S1 Fast Minara Model Benchmark (`max_tokens=64`, default `auto` effort)
Note: `minara/gpt-5.4-mini` is `status="excluded"` in `minara_models.json` and absent from `/v1/models`.

| Model | Status | Prompt 1 (`42`) | Prompt 2 (JSON) | Med TTFT / Total (ms) | Credits | Notes |
|---|---|---|---|---|---|---|
| `minara/claude-haiku-5-5` | PASS | 200 OK (`42`) | 200 OK | `1223.2 / 1223.5` | `0.0171` | **Fastest clean completion** |
| `minara/deepseek-v4-flash` | PASS | 200 OK (`42`) | 200 OK | `1207.5 / 2436.3` | `0.0185` | Clean, reliable |
| `minara/glm-5.3-flash` | PASS | 200 OK (`42`) | 200 OK | `2238.6 / 2241.1` | `0.0107` | Lowest cost; emits trailing `0/0` SSE usage chunk |
| `minara/gpt-6-luna-pro` | PASS | 200 OK (`42`) | 200 OK | `4764.1 / 4764.5` | `0.1303` | Clean, slower/costlier |
| `minara/gemini-3.8-flash` | PARTIAL | 200 (`finish=length`) | 200 (`finish=length`) | `2989.4 / 2989.6` | `0.1688` | Hidden thinking at default `medium` exhausted `max_tokens=64` |

**Recommendation**: Prefer `minara/claude-haiku-5-5` (fastest) or `minara/deepseek-v4-flash` (cleanest low-cost general fast model) for fast/auxiliary tasks.

### R5 Structured JSONL & R6 IDE CLI Smoke Tests
- **R5 JSONL**: **569 / 569** stdout lines valid JSONL (`invalid=0`), stderr `0` bytes, **0 secret leaks**.
- **Claude Code `2.1.330` (`--bare --print`)**: **PASS** (`minara/claude-haiku-5-5`, TTFT `1611 / 2080 ms`, `0.0430 cr`, `Read` tool verified). Note: `--bare` still reads `~/.claude/settings.json` `env`, so `--settings <temp.json>` overrides `ANTHROPIC_BASE_URL`.
- **Codex CLI `0.171.0` (`exec --ephemeral --json`)**: **PASS** (`minara/deepseek-v4-flash`, TTFT `1877 ms`, `0.2032 cr`).
- **OpenCode `1.19.6` (`run --pure --format json`)**: **PASS** (`minara/deepseek-v4-flash`, TTFT `2072 / 1758 ms`, `0.5295 cr`, `--file` verified).
- **DeepSeek Harness `0.2.2-alpha.3` (`--profile headless`)**: **PASS** (`minara/deepseek-v4-flash`, TTFT `1631 / 1419 ms`, `run_code` PTC passed under `--permission-mode danger-full-access`).
- **Claude Desktop sync dry-run (`sync_claude_desktop_catalog.py`)**: **PASS** (`behavesAs` emitted on `16/16` `minara/claude-*` rows; `supports1m: true` on all 13 1M-context models).

## 4. Retool `4.103.0` / `4.104.0` Cloud Gate & Claude Effort Fixes

- **Retool Cloud Gate Root Cause**: Retool deployed `assistCopilotStreamGateEnabled` on Cloud (`4.103.0-f4f6d56` / `4.104.0-526d066`), rejecting `POST /api/gpt/copilotStreamGPT` with `400 Invalid request format` when `metadata.sandpackAgent` is sent without a valid sandbox attestation, and returning `403 Assist is not available for this plan.` for Freetool orgs without Assist.
- **Fix (`7380257`) & Live Verification**:
  - `direct.py` falls back once to unmarked Assist on `400 Invalid request format` when `metadata` is present, and `apierr.py` + `direct.py` map `403 Assist is not available for this plan.` to `502 upstream_error` (`plan_refusal=True`, `unbilled=True`) with no `auth_retry` storm.
  - Verified live on isolated port `8788` against Retool `4.104.0-526d066`:
    1. Direct `claude-sonnet-5-5` on the Retool Freetool account hit `400` -> `upstream.assist_retry` -> `403` -> `502` (`plan_refusal=True`, `unbilled=True`), placing the account/model on a 5-minute `account.model_refused` hold.
    2. Repeat `claude-sonnet-5-5` returned `404` in `2 ms` with **0 upstream calls**.
    3. `smart-opus` route hit Retool `400 -> 403 -> account.failover (reason="plan_refused")` and seamlessly failed over to a healthy Minara account (`200 "OK"`, `0.2992 cr`).
    4. `55 / 55` stdout JSONL lines valid, `0` secret leaks.
- **Claude Desktop / Code Effort Fixes**:
  - `sync_claude_desktop_catalog.py` now auto-emits `behavesAs` and `supports1m` for prefixed (`minara/claude-*`) and virtual (`@<profile>`) Claude models so Claude Desktop displays the effort picker.
  - `minara_catalog.py` now populates `default_reasoning_effort` (`"medium"` / `"high"`) for Minara reasoning models under `reasoning_effort="auto"`.

## 5. Final Verification Summary

- `git diff --check 113e43e..HEAD`: passed (exit 0)
- `ruff check --no-cache .`: `All checks passed!` (exit 0)
- Full Python test suite (`python -B -m pytest -p no:cacheprovider -q --tb=short`): exited with code `0`, `[100%]` (`11,278` collected, `0` failures; `1` skipped marker observed at `18%`, `2` expected `PytestUnraisableExceptionWarning` in `tests/test_console_broken.py`).
- Web checks (verified on `213109a`, no `web/` changes since): `node --test tests/*.test.mjs` (`174/174`), `node scripts/accuracy_report.mjs` (`132/132`), `npx playwright test --reporter=line` (`206/206`).

## 6. Actionable User Follow-Ups

1. **Restart the live relay on port `8787`** so `main` (`HEAD`) changes take effect.
2. **Re-run `scripts/sync_claude_desktop_catalog.py`** to write `behavesAs` and `supports1m` into your Claude Desktop 3P config so the effort selector appears for `minara/claude-*` and virtual models.
3. **Optional (Claude Code)**: Add `CLAUDE_CODE_ALWAYS_ENABLE_EFFORT=1` to `~/.claude/settings.json` `env` if you want Claude Code's effort UI enabled for prefixed `minara/*` models.
4. **Optional (R3 Fast Models)**: Prefer `minara/claude-haiku-5-5` or `minara/deepseek-v4-flash` for fast/auxiliary routes.
