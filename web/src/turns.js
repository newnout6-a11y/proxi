/** Build traces client-side from the live journal so they update in real time. */
const elapsed = (from, to) => Math.max(0, (to - from) * 1000);

/** Same rule as the relay's metrics: a price is a finite, non-negative number. */
const isPrice = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0;

/** Length in ms of the union of [start, end] spans given in seconds: time covered twice counts once. */
function unionMs(spans) {
  let total = 0;
  let covered = -Infinity;
  for (const [start, end] of spans.filter(([from, to]) => to > from).sort((a, b) => a[0] - b[0])) {
    if (end <= covered) continue;
    total += end - Math.max(start, covered);
    covered = end;
  }
  return total * 1000;
}

function closeText(trace, at) {
  if (trace.textStarted !== null) trace.textMs += elapsed(trace.textStarted, at);
  trace.textStarted = null;
}

function closeThinking(trace, at) {
  if (trace.thinkingStarted !== null) trace.thinking += elapsed(trace.thinkingStarted, at);
  trace.thinkingStarted = null;
}

function closeTools(trace, at) {
  if (!trace.toolOpen.size) return;
  for (const start of trace.toolOpen.values()) trace.toolSpans.push([start, Math.max(start, at)]);
  trace.toolOpen.clear();
  trace.args = unionMs(trace.toolSpans);
}

function closeExec(trace, at) {
  if (trace.execStarted !== null) trace.execMs += elapsed(trace.execStarted, at);
  trace.execStarted = null;
}

function closeWait(trace, at) {
  if (trace.waitStarted !== null) trace.waitMs += elapsed(trace.waitStarted, at);
  trace.waitStarted = null;
}

/** The first upstream signal of an attempt ends that attempt's waiting. */
function signal(trace, at) {
  closeWait(trace, at);
  trace.firstSignal ??= at;
}

const toolKey = (data) => data.call_id ?? data.name ?? '';

/** One paid upstream attempt, as the relay's journal keeps it: did it report usage, and did a valid price come with it. */
function attemptOf(trace, number) {
  let attempt = trace.costAttempts.get(number);
  if (!attempt) trace.costAttempts.set(number, attempt = { observed: false, known: false });
  return attempt;
}

export function buildTraces(events) {
  const map = new Map();
  for (const event of events) {
    if (event.kind === 'relay.start') {
      // A restart ends every turn the previous process still had running: no turn.end will ever come.
      for (const trace of map.values()) if (trace.status === 'running') trace.status = 'interrupted';
      continue;
    }
    if (!event.turn_id) continue;
    let trace = map.get(event.turn_id);
    if (!trace) {
      trace = { id: event.turn_id, started: event.ts, ended: null, status: 'running', restored: false, model: null, effort: null,
        task: null, askedEffort: null, outputTokens: null, tokensPerSecond: null, speedDuration: null, speedReason: null,
        transport: null, dialect: null, ttft: null, duration: null, credits: null, creditsSum: null, creditsComplete: false,
        phase: 'wait', phaseStarted: event.ts, waitStarted: event.ts, waitMs: 0, firstSignal: null,
        thinkingStarted: null, thinking: 0, textStarted: null, textMs: 0, toolOpen: new Map(), toolSpans: [], args: 0,
        execStarted: null, execMs: 0, inputTokens: 0, cacheRead: 0, cacheWrite: 0,
        tools: 0, commands: 0, errors: 0, last: event.ts, events: [],
        costAttempts: new Map(), costAttempt: null };
      map.set(event.turn_id, trace);
    }
    const data = event.data || {};
    if (event.restored === true) trace.restored = true;
    if (event.kind === 'upstream.usage_start') {
      closeText(trace, event.ts);
      closeThinking(trace, event.ts);
      closeTools(trace, event.ts);
      closeExec(trace, event.ts);
      // An attempt that never signalled waited until the next one began; the new one waits for its own signal.
      closeWait(trace, event.ts);
      trace.waitStarted = event.ts;
      trace.phase = 'wait'; trace.phaseStarted = event.ts;
      trace.costAttempt = Number.isInteger(data.attempt) ? data.attempt : Math.max(-1, ...trace.costAttempts.keys()) + 1;
      attemptOf(trace, trace.costAttempt);
    }
    trace.events.push(event);
    trace.last = Math.max(trace.last, event.ts);
    if (event.kind === 'turn.begin') {
      Object.assign(trace, { model: data.model, effort: data.effort, transport: data.transport, dialect: data.dialect });
      // What the request is for (a client compacting its history, the dearest turn when the cache does not continue),
      // and the effort the client asked for when the relay ran the turn on the dialog's own to keep that cache.
      trace.task = typeof data.client_task === 'string' ? data.client_task : null;
      trace.askedEffort = data.effort_resolution === 'cache_continuity' ? data.requested_effort ?? null : null;
    }
    if (event.kind === 'turn.end') {
      closeText(trace, event.ts);
      closeThinking(trace, event.ts);
      closeTools(trace, event.ts);
      closeExec(trace, event.ts);
      closeWait(trace, event.ts);
      Object.assign(trace, { ended: event.ts, duration: data.duration_ms, status: data.reason || 'stop' });
      // A client that cancels does not wait for the telemetry: like the relay's journal, count the attempt that was
      // silent at this moment as possibly billed, for good (what arrives later does not undo or repeat it)
      const last = trace.costAttempts.get(trace.costAttempt);
      if (data.reason === 'cancelled' && last) last.observed = true;
    }
    if (event.kind === 'request.speed') {
      // One final measurement covers all account failovers; turn.end only times this account's timeline.
      trace.outputTokens = data.output_tokens ?? null;
      trace.tokensPerSecond = data.tokens_per_second ?? null;
      trace.speedDuration = data.duration_ms ?? null;
      trace.speedReason = data.reason ?? null;
    }
    if (event.kind === 'upstream.first_token') {
      signal(trace, event.ts);
      trace.ttft = data.ttft_ms;
      if (data.first === 'thinking' && trace.thinkingStarted === null) trace.thinkingStarted = event.ts;
      trace.phase = data.first === 'thinking' ? 'think' : 'other';
      trace.phaseStarted = event.ts;
    }
    if (event.kind === 'upstream.usage') {
      // The price is the only thing that makes an attempt known: the relay's reported flag alone never does
      const attempt = attemptOf(trace, Number.isInteger(data.attempt) ? data.attempt : trace.costAttempt ?? 0);
      attempt.observed = true;
      if (isPrice(data.credits)) {
        attempt.known = true;
        trace.creditsSum = (trace.creditsSum ?? 0) + data.credits;
      }
      trace.inputTokens += Number(data.input_tokens) || 0;
      trace.cacheRead += Number(data.cache_read) || 0;
      trace.cacheWrite += Number(data.cache_write) || 0;
    }
    if (event.kind === 'upstream.thinking_start') {
      closeText(trace, event.ts);
      closeTools(trace, event.ts);
      signal(trace, event.ts);
      trace.phase = 'think'; trace.phaseStarted = event.ts; trace.thinkingStarted ??= event.ts;
    }
    if (event.kind === 'upstream.thinking_end') {
      trace.thinking += Number(data.thinking_ms) || 0; trace.thinkingStarted = null;
      trace.phase = 'other'; trace.phaseStarted = event.ts;
    }
    if (event.kind === 'upstream.text_start') {
      signal(trace, event.ts);
      closeThinking(trace, event.ts);
      closeText(trace, event.ts);
      closeTools(trace, event.ts);
      trace.textStarted = event.ts; trace.phase = 'out'; trace.phaseStarted = event.ts;
    }
    if (event.kind === 'upstream.tool_start') {
      closeText(trace, event.ts);
      closeThinking(trace, event.ts);
      signal(trace, event.ts);
      if (!trace.toolOpen.has(toolKey(data))) trace.toolOpen.set(toolKey(data), event.ts);
      trace.phase = 'tools'; trace.phaseStarted = event.ts;
    }
    if (event.kind === 'tool.call') {
      closeText(trace, event.ts);
      closeThinking(trace, event.ts);
      trace.phase = 'other'; trace.phaseStarted = event.ts;
      trace.tools += 1;
      // Parallel calls overlap: keep every call as a span and count their union, not the sum of their lengths.
      const opened = trace.toolOpen.get(toolKey(data));
      const argsMs = Number(data.args_ms);
      if (argsMs > 0) trace.toolSpans.push([event.ts - argsMs / 1000, event.ts]);
      else if (opened !== undefined) trace.toolSpans.push([opened, event.ts]);
      trace.toolOpen.delete(toolKey(data));
      trace.args = unionMs(trace.toolSpans);
    }
    if (event.kind === 'exec.start') {
      // A local command is not the model writing: close what was open, the command is timed on its own.
      closeText(trace, event.ts);
      closeThinking(trace, event.ts);
      closeTools(trace, event.ts);
      trace.execStarted = event.ts;
      trace.phase = 'exec'; trace.phaseStarted = event.ts;
    }
    if (event.kind === 'exec.result') {
      trace.commands += 1;
      if (Number.isFinite(data.duration_ms) && data.duration_ms >= 0) {
        trace.execMs += data.duration_ms;
        trace.execStarted = null;
      } else closeExec(trace, event.ts);
      trace.phase = 'other'; trace.phaseStarted = event.ts;
    }
    if (event.level === 'error') trace.errors += 1;
  }
  return [...map.values()].map((trace) => {
    const status = trace.status === 'running' && trace.restored ? 'interrupted' : trace.status;
    const cost = costOf(trace, status);
    return { ...trace, status, credits: cost.credits, creditsComplete: cost.complete };
  }).reverse();
}

/** Completed request throughput, not decoding speed: includes waiting, thinking, retries and relay commands. */
export function speedOf(trace) {
  const rate = trace.tokensPerSecond;
  return ['stop', 'tool_calls', 'length'].includes(trace.status)
    && Number.isInteger(trace.outputTokens) && trace.outputTokens >= 0
    && ['stop', 'tool_calls', 'length'].includes(trace.speedReason)
    && typeof trace.speedDuration === 'number' && Number.isFinite(trace.speedDuration) && trace.speedDuration > 0
    && typeof rate === 'number' && Number.isFinite(rate) && rate >= 0 ? rate : null;
}

/**
 * What a turn cost, by the relay's own rules (journal._Turn, as /admin/api/turns reports it): an attempt that reported
 * usage without a valid price is unknown, and so is the attempt a client cancelled while it was silent (marked when
 * the turn ended, see buildTraces); one that only started (refused, 429/500, never left) cost nothing. `credits` is
 * the sum of the valid prices, else 0 for a turn that ended without an unknown attempt (also one that never reached
 * upstream), else null. `complete` holds only for a turn whose end the relay recorded and that has no unknown
 * attempt, so a running or interrupted turn is never complete: its price so far is a lower bound.
 */
export function costOf(trace, status = trace.status) {
  const running = status === 'running';
  const ended = !running && status !== 'interrupted';
  const attempts = [...trace.costAttempts.values()];
  const unknown = attempts.filter((attempt) => attempt.observed && !attempt.known).length;
  const priced = attempts.some((attempt) => attempt.known);
  return { credits: priced ? trace.creditsSum : ended && !unknown ? 0 : null, unknown, running, complete: ended && !unknown };
}

/** A turn silent for longer than this is not running any more: the relay's own turn timeout is 15 minutes by default. */
export const STALE_AFTER = 20 * 60;

/** What a raised turn timeout gets on top of it, for the turn's end to arrive and be journaled. */
export const STALE_MARGIN = 5 * 60;

/**
 * How long a running turn may stay silent before it counts as interrupted: the relay ends a turn itself after its
 * `turn_timeout` (an operator may raise it to a day), so a turn within it is not lost. Never shorter than STALE_AFTER,
 * which is also what an unknown timeout (an older relay) gets.
 */
export function staleLimit(turnTimeout) {
  return typeof turnTimeout === 'number' && Number.isFinite(turnTimeout) && turnTimeout > 0
    ? Math.max(STALE_AFTER, turnTimeout + STALE_MARGIN) : STALE_AFTER;
}

/**
 * A turn still 'running' after that much silence (`staleAfter`, in seconds: see staleLimit) is 'interrupted' (a relay
 * restart is already recognised while the traces are built). It ends at its last event, not at the clock.
 */
export function statusAt(trace, now = Date.now() / 1000, staleAfter = STALE_AFTER) {
  return trace.status === 'running' && now - trace.last > staleAfter ? 'interrupted' : trace.status;
}

/** What a turn is for, when the relay recognised a request that is more than a chat turn (the journal's client_task). */
export const TASK_LABEL = { compaction: 'сжатие' };

/** "effort max", and what the client asked for when the relay ran the turn on the dialog's own effort to keep its cache. */
export function effortText(trace) {
  if (!trace.effort) return '';
  const asked = trace.askedEffort && trace.askedEffort !== trace.effort
    ? ` (клиент ${trace.askedEffort === 'auto' ? 'по умолчанию' : trace.askedEffort})` : '';
  return `effort ${trace.effort}${asked}`;
}

export const PHASES = [
  { key: 'wait', label: 'ожидание', title: 'До первого события upstream' },
  { key: 'think', label: 'мышление', title: 'Сигнал thinking от upstream' },
  { key: 'tools', label: 'аргументы инструментов', title: 'Генерация аргументов инструментов' },
  { key: 'out', label: 'ответ', title: 'Вывод ответа' },
  { key: 'other', label: 'без детализации', title: 'Длительность без подтверждённой фазы: паузы между попытками и локальные команды' },
];

/**
 * Phase lengths in ms: waiting, thinking, tool arguments, output. What is left over is `other`, and `exec`
 * (local commands) is the part of it that is known to be a command.
 */
export function phasesOf(trace, now = Date.now() / 1000, staleAfter = STALE_AFTER) {
  const status = statusAt(trace, now, staleAfter);
  const open = status === 'running';
  const end = open ? Math.max(now, trace.last) : trace.ended ?? trace.last;
  const duration = Number.isFinite(trace.duration) ? trace.duration : elapsed(trace.started, end);
  const rawThink = Math.max(0, trace.thinking || 0) + (trace.thinkingStarted !== null ? elapsed(trace.thinkingStarted, end) : 0);
  const rawTools = trace.toolOpen.size
    ? unionMs([...trace.toolSpans, ...[...trace.toolOpen.values()].map((start) => [start, end])]) : Math.max(0, trace.args || 0);
  const rawWait = trace.firstSignal !== null
    ? trace.waitMs + (trace.waitStarted !== null ? elapsed(trace.waitStarted, end) : 0)
    : (rawThink || rawTools ? 0 : duration);
  const wait = Math.min(duration, rawWait);
  const think = Math.min(duration - wait, rawThink);
  const tools = Math.min(duration - wait - think, rawTools);
  const known = wait + think + tools;
  const remaining = Math.max(0, duration - known);
  const out = Math.min(remaining, trace.textMs + (trace.textStarted !== null ? elapsed(trace.textStarted, end) : 0));
  const other = remaining - out;
  const exec = Math.min(other, trace.execMs + (trace.execStarted !== null ? elapsed(trace.execStarted, end) : 0));
  const total = duration;
  return { wait, think, tools, out, other, exec, total, open, status };
}

/**
 * The shared scale of the turn tapes is the longest finished turn: a running one grows with the clock and would
 * squeeze every other tape as it ticks. With nothing finished to measure by, the running turns set the scale
 * themselves, so that a short one is still shorter than a long one instead of every tape being full.
 */
export function scaleOf(traces, now = Date.now() / 1000, staleAfter = STALE_AFTER) {
  const all = traces.map((trace) => phasesOf(trace, now, staleAfter));
  const finished = all.filter((phases) => !phases.open);
  return Math.max(1, ...(finished.length ? finished : all).map((phases) => phases.total));
}
