# Proxi administration design — 2026-11-05

## Direction

- Product: a local desk for routing model requests and understanding their cost.
- Character: measured, legible, exact.
- World: an operator's instrument panel, graphite surfaces and an amber signal.
- Moments: the real credit/cache charts; the existing Stageshow routing example.
- Signature: the shared phase tape connecting a turn's status, timing and events.

The existing palette, fonts, section order and Stageshow kit were retained. The
baseline was already designed; this pass completes missing controls, readable
states and mobile layouts instead of changing its identity arbitrarily.

## Changes

Settings now include a group index and a real system-policy editor: standalone,
append or replace, multiline role/boundaries, explicit empty policy and reset to
the built-in role (`null`). The backend publishes the built-in policy for preview.
Settings retain edits made while a previous save is pending and show loading,
failure, dirty and restart-required states. The catalog labels context, effort,
verbosity and availability; controls remain readable on narrow displays.

Proxi identity, quieter labels, visible journal connection status, destructive
account actions and more legible semantic text complete the shared chrome.
Contrast changes live in the existing token file. The pool illustration is
labelled as an example, separate from live account figures. Existing animations
and their kit geometry were not redesigned.

The display also fixes misleading telemetry: unknown cost stays unknown;
reported cost plus a missing attempt is a lower bound. Token snapshots of an
already billed attempt do not create a second unknown charge. Journal usage
events now carry attempt identity. Long-running turns advance on a local clock;
thinking/output/tool intervals close at phase and attempt boundaries. Gaps are
labelled unclassified instead of being attributed to output. Cache-write spikes
are observations, without an unproven claim that TTL expired. Activity tabs and
chart time selection support the keyboard.

> **Correction (2026-11-05, PR #24 review).** Two statements above are replaced:
>
> - "The backend publishes the built-in policy for preview": the relay has two built-in
>   roles, and `/admin/api/settings` publishes both in `default_system_prompts`, each with
>   the requests it serves: the relay role (requests without tools and without client
>   instructions, and the `append`/`replace` modes) and the role for clients with their own
>   tools. `default_system_prompt` still carries the relay role alone. While the standard
>   role is selected, Settings show both; a custom role replaces both.
> - "Gaps are labelled unclassified": the phase tape names time without a confirmed phase
>   «без детализации» (pauses between attempts and local commands). A local command is timed
>   on its own («команды») and is not counted as output.

## Validation

Browser plugin was not available; the repository's Playwright workflow used the
installed Google Chrome. Test APIs use synthetic fixtures, without production
credentials, provider calls or configuration writes.

Commands run:

```powershell
uv run --python 3.14 --with-requirements requirements.txt --no-project python -m pytest -o addopts= -q --tb=short
cd web
npm run build
node --test tests/turns.test.mjs
$env:PW_CHROMIUM='C:\Program Files\Google\Chrome\Application\chrome.exe'
npx playwright test --reporter=line
node scripts/accuracy_report.mjs
```

- Python: **1,968 passed in 225.40 seconds**.
- Trace model: **12 passed**, including snapshots/retries and multiple phases.
- Browser flows: **29 passed in 37.6 seconds**, including null versus empty role,
  in-flight save edits, keyboard tabs/charts and 280/320/414 px overflow checks.
- Rendered gate: **60/60 checks passed** across light/dark themes, five screens,
  editor default/hover/focus/selected/editing states, four widths and runtime
  console checks. Axe WCAG-tagged checks exclude the faux illustration internals;
  this is not a claim of a complete manual accessibility certification.
- Stageshow reduced-motion poster gates passed at **1440 and 390 px**. The
  existing full loop capture was **inconclusive** because sampling gaps prevented
  continuous motion judgement. Its Count entrance warning and incomplete capture
  are retained, not counted as a successful animation audit.

> **Correction (2026-11-05, PR #24 review).** The figures above are those of the first
> pass. After the review fixes the same commands give:
>
> - Python: **3154 passed in 293.70 seconds** (was 1,968).
> - Trace model, `node --test tests/turns.test.mjs`: **70 passed** (was 12). The file now
>   also covers the cost rules of a turn, the metrics coverage and the burn rate, and the
>   interruption of silent turns.
> - Browser flows: **77 passed in 58 seconds** (was 29).
> - Rendered gate: **60/60 checks passed**, the same count; its fixtures now also carry a
>   partial pool balance and a route that ignores effort.

Screenshots, traces and gate JSON stay outside the repository under
`%TEMP%/proxi-design-qa`, `%TEMP%/relay-admin-playwright` and
`%TEMP%/opencode/admin-shots/design-*-20261105`.

## Windows CI follow-up

The first design CI run failed in a legacy streaming fixture with WinError10038.
A deterministic regression then reproduced the underlying ownership defect:
the fallback cancellation wrapper closed a raw socket handle while its original
Python owner still retained the number. A later owner finalizer could close a
reused handle belonging to another connection or an event loop.

The fallback now borrows the handle for shutdown and detaches its temporary
wrapper instead of stealing ownership. It is explicitly best effort for unknown
response shapes; Windows may require an owning close to unblock a pending read.
Real HTTPResponse objects retain their existing `fp.raw._sock` owning-close path.
The old cancellation fixture now represents that real ownership chain, while a
separate regression protects unowned handles. This is not a retry-only CI fix.
Focused validation: **72 passed in 4.08 seconds** with:

```powershell
uv run --python 3.14 --with-requirements requirements.txt --no-project python -m pytest tests/test_stream_safety.py tests/test_pr2_hardening.py tests/test_shutdown.py tests/test_project_workflow.py -o addopts= -q --tb=short
```

## Credits

Design process: [Unslop](https://heydamir.design/unslop) by
[Damir Sokolovsky](https://x.com/heydamir), CC BY 4.0. Existing demonstration kit:
[Stageshow](https://github.com/benjitaylor/skills), MIT; source notices remain in
`web/src/stage/NOTICE.md` and `LICENSE`.
