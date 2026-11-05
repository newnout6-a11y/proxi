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
    if (event.kind === 'turn.begin') Object.assign(trace, { model: data.model, effort: data.effort, transport: data.transport, dialect: data.dialect });
    if (event.kind === 'turn.end') {
      closeText(trace, event.ts);
      closeThinking(trace, event.ts);
      closeTools(trace, event.ts);
      closeExec(trace, event.ts);
      closeWait(trace, event.ts);
      Object.assign(trace, { ended: event.ts, duration: data.duration_ms, status: data.reason || 'stop' });
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

/**
 * What a turn cost, by the relay's own rules (journal._Turn, as /admin/api/turns reports it): an attempt that reported
 * usage without a valid price is unknown, and so is the silent attempt of a turn the client cancelled; one that only
 * started (refused, 429/500, never left) cost nothing. `credits` is the sum of the valid prices, else 0 for a turn
 * that ended without an unknown attempt (also one that never reached upstream), else null. `complete` holds only for
 * a turn whose end the relay recorded and that has no unknown attempt, so a running or interrupted turn is never
 * complete: its price so far is a lower bound.
 */
export function costOf(trace, status = trace.status) {
  const running = status === 'running';
  const ended = !running && status !== 'interrupted';
  const attempts = [...trace.costAttempts.values()];
  const latest = trace.costAttempts.get(trace.costAttempt);
  const silent = status === 'cancelled' && latest !== undefined && !latest.observed;
  const unknown = attempts.filter((attempt) => attempt.observed && !attempt.known).length + (silent ? 1 : 0);
  const priced = attempts.some((attempt) => attempt.known);
  return { credits: priced ? trace.creditsSum : ended && !unknown ? 0 : null, unknown, running, complete: ended && !unknown };
}

/** A turn silent for longer than this is not running any more: the relay's own turn timeout is 15 minutes by default. */
export const STALE_AFTER = 20 * 60;

/**
 * A turn still 'running' after that much silence is 'interrupted' (a relay restart is already recognised while the
 * traces are built). It ends at its last event, not at the clock.
 */
export function statusAt(trace, now = Date.now() / 1000) {
  return trace.status === 'running' && now - trace.last > STALE_AFTER ? 'interrupted' : trace.status;
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
export function phasesOf(trace, now = Date.now() / 1000) {
  const status = statusAt(trace, now);
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
 * The shared scale of the turn tapes is the longest finished turn: a running one grows with the clock
 * and would squeeze every other tape as it ticks.
 */
export function scaleOf(traces, now = Date.now() / 1000) {
  const finished = traces.map((trace) => phasesOf(trace, now)).filter((phases) => !phases.open);
  return Math.max(1, ...finished.map((phases) => phases.total));
}
