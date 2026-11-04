# Rehearsal: login hero and empty states

## Gold copied

- Login hero: `hero-demo` (stage 720×340, well with 32px gutter, window pinned and
  running off the right and bottom, a satellite straddling its edge; compact design
  at 358 wide) and `payout-flow` (no cursor, overlapping arrivals, payoff that holds).
- Empty states: `static-card` (window pinned top-left, real words only on the focal row).

## What changed

- The product is the relay: an IDE terminal sends a request, a new row opens in the
  relay journal and pushes older rows down, the row's turn tape fills phase by phase,
  the credits figure rolls down by the turn's cost. Payoff holds 3.2 s; reset is
  caused by the terminal clearing for the next request, then the scene snaps back.
- Real words: the command, `claude-opus-5-5`, `200`, `21,8 с`, the credits figure.
  Everything else is filler bars.
- Look: graphite stage tokens, one amber accent (`--ss-accent`, soft and text set
  explicitly), app fonts Manrope and IBM Plex Mono.
- Kit parts are imported from their own files (not `kit/index.ts`) so only the parts
  in use bring CSS into the admin bundle.

## Gate

```
film.mjs http://127.0.0.1:4174/admin/ --select ".login-hero .ss-fit" --gate --width 1440
rehearsal passed
film.mjs ... --width 390
rehearsal passed
```

Contact sheets were reviewed frame by frame at both widths. Fixed during rehearsal:
caret touching the typed text, typed text not marked `data-typing`, credits figure
popping in, uneven compact gutters, compact window narrower than tall, terminal
covering the focal row's tape on 390 px.

Fixed after the independent review: the balance no longer rolls back up in view
at the loop seam (it fades out, is reset while hidden, and fades back in).

## Not checked / remaining warnings

- `rehearsal passed` means the never-intentional lines are at zero. Film still
  reports `inconclusive` coverage (sampling gaps on this machine) and these
  non-gate warnings, which are not zero:
  - `appears with no entrance` and `settle snap` on `.ss-count` / its digits: the
    kit's own digit strips homing back to the middle decade after a roll;
  - `settle snap` on `.ss-term__caret` (6.6 px at 1440, 15.8 px at 390, the stage
    is scaled there): the block caret stepping with the last typed letter;
  - at 390 px, `near-empty frames` for about 0.8 s at the loop reset.
- Empty-state stills were checked by screenshots, not by film.

# Rehearsal: account pool scene and add-account guide

## Gold copied

- Pool scene (`PoolScene.jsx`, Accounts page): `subscriptions` (a system story with
  no cursor, a chip budding off its source with `Fly held` and the row's own pill
  taking over on landing, a figure rolling after it) on the `hero-demo` frame (window
  pinned one gutter in, running off the right and bottom, a terminal satellite over
  the window's sidebar only). Compact design at 358 drops the sidebar and the toggles
  and parks the terminal over the filler rows.
- Add-account guide (`AddGuide.jsx`, in the form): `static-card` (a browser window
  pinned top-left, real words only on the address and the two request headers).

## What changed

- The story is the pool's routing rule: a turn's cost leaves the IDE, lands on the
  enabled account with the lowest priority, its balance rolls under the reserve
  threshold and it steps back; the next turn goes to the spare account; the
  switched-off account never receives one. Poster: main in reserve, spare serving.
- The guide pairs numbered accent badges (kit `Badge`) with the form fields of the
  same number: base and page_uuid from the editor address, `cookie` and
  `x-xsrf-token` from the request headers (the headers the relay itself sends).
- Light theme uses the dark amber as accent text (`stageTokens(theme)`), the
  terminal keeps the light amber on its dark surface.

## Gate

```
film.mjs <preview>?scene=pool  --select "#example .ss-fit" --gate --width 1440   rehearsal passed
film.mjs <preview>?scene=pool  ... --width 390                                    rehearsal passed
film.mjs <preview>?scene=pool&theme=light ... --width 1440                        rehearsal passed
film.mjs <preview>?scene=guide[&theme=light] --still --gate --width 1440 / 390    rehearsal passed, no issues
```

Fixed during rehearsal: terminal covering the active nav label, whole rows fading
at the loop seam (now only the changed figures fade), the source pill vanishing in
one frame, toggle inset, placeholder line air, compact cut amounts and terminal
padding, the guide's cut page_uuid, touching placeholder bars, pale amber text on
the light ground.

## Not checked / remaining warnings

- Non-gate warnings left on the pool scene: the chip-to-pill handoff (`appears with
  no entrance` / `vanishes with no exit`), which is the gold's own instant handoff;
  the kit `Count` digit strips; about 140 ms of `near-empty frames` at the seam.
- The rehearsal preview page was a temporary Vite entry, not part of the build.
