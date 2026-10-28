# Verify2 (adversarial) — last stage 5dfb3a09 + 3e63663 + 36c7eda

Read-only. Scratch harnesses in .verify2 only. No retool.com traffic.

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| A | long exec loop no longer inflates prompt; single task substitution; output trimmed by max_prompt_bytes | FAIL (partial) | Task text once: orchestrator.py:152-153, measured p.count("USER TASK")==1 at every step, p.count(task)==1. Limit NOT enforced: .verify2/a_prompt.py limit=1200 -> lens 791,1387,1396,1417,1438 (steps 2-5 over limit); .verify2/a4_deep.py limit=24000, 3000-char cmds, max_exec_turns=10 -> 25395..37464 over limit from step 7. Cause: orchestrator.py:157-158 budget ignores trail/wrapper/reminder + max(600,..) floor; trail (orchestrator.py:154-155) keeps full command text, never trimmed |
| B | no USER TASK resubstitution in format_command_followup | PASS | executor.py:223-230 emits no USER TASK; .verify2/bcd.py: "own USER TASK: False", body shown |
| C | non-stream cancels on disconnect | PASS | proxy.py:298-305 session.cancel() + fut.cancel(); .verify2/bcd.py: _collect -> cancelled, cancel calls 1, fut.cancelled True; also with holder session None (proxy.py:301) -> no AttributeError |
| D | shared lock not leaked on exception/cancel in acquire; per_request new thread per acquire | FAIL (partial) | Shared: session.py:360-370 except BaseException releases lock+slot; .verify2/bcd.py D1 slots 1 -> 2, re-acquire ok. per_request: session.py:371-375 catches only Exception; cancel inside create_thread leaks slot permanently — D2 slots 2 -> 1, not restored (CancelledError is BaseException) |
| E | no regressions: pytest green, per_request isolation, prompt keeps [ROLE] + [LOCAL EXEC REMINDER] | PASS with caveat | python -m pytest -q: 90 collected, exit 0, no F/E; isolation test_pool_shared.py:50-60 passes and release closes per_request session (session.py:380-381); [ROLE] present on all prompts; [LOCAL EXEC REMINDER] present only on the first prompt of a turn (orchestrator.py:66-67, dropped by orchestrator.py:161) |

## Defects, by severity

1. CRITICAL — RelaySession._cancelled is never reset (session.py:121,125; no reset in acquire/release/stream).
   Risk: in session_mode=shared one client disconnect poisons the pool forever: every later turn yields only {"type":"end","reason":"cancelled"} while still pushing the prompt upstream.
   Check: python .verify2/shared_cancel.py -> _cancelled True, turn2 and turn3 return [{'type':'end','reason':'cancelled'}], ws.sent==1 (prompt sent).
2. CRITICAL — cancelled turn's ws frames are never drained (session.py:208-210 returns before reading).
   Risk: shared mode cross-talk — the next turn consumes the aborted turn's leftovers; a leftover agent_loop_done terminates the next turn instantly with empty answer.
   Check: python .verify2/crosstalk2.py -> leftovers 1, turn2 -> [{'type':'final_text','text':''},{'type':'end','reason':'stop'}].
3. HIGH — per_request slot leak on cancel during create_thread (session.py:372; acquire sits outside the try/finally in orchestrator.py:78-80); _slots is an unbounded asyncio.Semaphore (session.py:347), leak is invisible and acquire has no timeout -> after N leaks every request blocks forever.
   Check: python .verify2/bcd.py D2 (slots 2 -> 1, never restored).
4. MEDIUM — max_prompt_bytes not enforced (see A): budget arithmetic ignores trail + "[Command result ...]" wrapper + reminder, floor max(600, budget), trail grows by full command length per step. Prompt limits configurable max_exec_turns irrelevant.
   Check: .verify2/a_prompt.py, .verify2/a4_deep.py.
5. MEDIUM — cancel only sets a local flag; nothing stops the upstream turn (no stop frame on ws), so the aborted turn keeps consuming credits and the thread stays busy; the next prompt is queued into it.
6. MEDIUM — double release inflates slots: release() always releases (session.py:378-388), lock double-release is swallowed by except RuntimeError but the semaphore is not bounded. Check: .verify2/bcd.py D3 -> "3 of 2".
7. LOW — dead lease helpers _SharedLease/_LeasedSession (session.py:399-425) call pool._release, which does not exist (hasattr false) -> AttributeError if wired up.
8. LOW — [LOCAL EXEC REMINDER] lost on every exec follow-up (orchestrator.py:161); same as pre-fix, but claim E is only true for the first prompt.
9. LOW — sse.client_gone returns True on any OSError/ValueError (sse.py:56-58), so a transient socket error counts as a disconnect; and _collect returns "cancelled" while proxy.py:277-278 still writes a 200 payload to the dead socket.

## Stage verdict
NOT closed. B/C hold; A's csompressia part holds but the byte limit does not; D fails for per_request; defects 1-2 make session_mode=shared unusable after the first disconnect in this stage.
