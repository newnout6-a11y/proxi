/** Build traces client-side from the live journal so they update in real time. */
function closeText(trace, at) {
  if (trace.textStarted !== null) trace.textMs += Math.max(0, (at - trace.textStarted) * 1000);
  trace.textStarted = null;
}

function closeThinking(trace, at) {
  if (trace.thinkingStarted !== null) trace.thinking += Math.max(0, (at - trace.thinkingStarted) * 1000);
  trace.thinkingStarted = null;
}

function closeTools(trace, at) {
  if (trace.toolStarted !== null) trace.args += Math.max(0, (at - trace.toolStarted) * 1000);
  trace.toolStarted = null;
}

export function buildTraces(events) {
  const map = new Map();
  for (const event of events) {
    if (!event.turn_id) continue;
    let trace = map.get(event.turn_id);
    if (!trace) {
      trace = { id: event.turn_id, started: event.ts, ended: null, status: 'running', model: null, effort: null,
        transport: null, dialect: null, ttft: null, duration: null, credits: null, creditsComplete: true, phase: 'wait', phaseStarted: event.ts,
        thinkingStarted: null, textStarted: null, toolStarted: null, textMs: 0, firstSignal: null, inputTokens: 0, cacheRead: 0, cacheWrite: 0,
        tools: 0, commands: 0, errors: 0, thinking: 0, args: 0, last: event.ts, events: [],
        costAttempts: new Map([[0, false]]), costAttempt: 0 };
      map.set(event.turn_id, trace);
    }
    const data = event.data || {};
    if (event.kind === 'upstream.usage_start') {
      closeText(trace, event.ts);
      closeThinking(trace, event.ts);
      closeTools(trace, event.ts);
      trace.phase = 'wait'; trace.phaseStarted = event.ts;
      if (trace.costAttempt === 0) trace.costAttempts.delete(0);
      trace.costAttempt = data.attempt;
      trace.costAttempts.set(trace.costAttempt, false);
    }
    trace.events.push(event);
    trace.last = Math.max(trace.last, event.ts);
    if (event.kind === 'turn.begin') Object.assign(trace, { model: data.model, effort: data.effort, transport: data.transport, dialect: data.dialect });
    if (event.kind === 'turn.end') {
      closeText(trace, event.ts);
      closeThinking(trace, event.ts);
      closeTools(trace, event.ts);
      Object.assign(trace, { ended: event.ts, duration: data.duration_ms, status: data.reason || 'stop' });
    }
    if (event.kind === 'upstream.first_token') {
      trace.firstSignal ??= event.ts;
      trace.ttft = data.ttft_ms;
      if (data.first === 'thinking' && trace.thinkingStarted === null) trace.thinkingStarted = event.ts;
      trace.phase = data.first === 'thinking' ? 'think' : 'other';
      trace.phaseStarted = event.ts;
    }
    if (event.kind === 'upstream.usage') {
      const attempt = data.attempt ?? trace.costAttempt;
      if (attempt !== 0) trace.costAttempts.delete(0);
      if (typeof data.credits === 'number' && Number.isFinite(data.credits)) trace.credits = (trace.credits ?? 0) + data.credits;
      trace.costAttempts.set(attempt, trace.costAttempts.get(attempt) === true
        || data.credits_reported === true || (typeof data.credits === 'number' && Number.isFinite(data.credits)));
      trace.inputTokens += Number(data.input_tokens) || 0;
      trace.cacheRead += Number(data.cache_read) || 0;
      trace.cacheWrite += Number(data.cache_write) || 0;
    }
    if (event.kind === 'upstream.thinking_start') {
      closeText(trace, event.ts);
      closeTools(trace, event.ts);
      trace.firstSignal ??= event.ts;
      trace.phase = 'think'; trace.phaseStarted = event.ts; trace.thinkingStarted ??= event.ts;
    }
    if (event.kind === 'upstream.thinking_end') {
      trace.thinking += Number(data.thinking_ms) || 0; trace.thinkingStarted = null;
      trace.phase = 'other'; trace.phaseStarted = event.ts;
    }
    if (event.kind === 'upstream.text_start') {
      trace.firstSignal ??= event.ts;
      closeThinking(trace, event.ts);
      closeText(trace, event.ts);
      closeTools(trace, event.ts);
      trace.textStarted = event.ts; trace.phase = 'out'; trace.phaseStarted = event.ts;
    }
    if (event.kind === 'upstream.tool_start') {
      closeText(trace, event.ts);
      closeThinking(trace, event.ts);
      trace.firstSignal ??= event.ts;
      trace.toolStarted ??= event.ts;
      trace.phase = 'tools'; trace.phaseStarted = event.ts;
    }
    if (event.kind === 'tool.call') {
      closeText(trace, event.ts);
      closeThinking(trace, event.ts);
      trace.phase = 'other'; trace.phaseStarted = event.ts;
      trace.tools += 1;
      trace.args += Number(data.args_ms) || (trace.toolStarted !== null ? Math.max(0, (event.ts - trace.toolStarted) * 1000) : 0);
      trace.toolStarted = null;
    }
    if (event.kind === 'exec.result') trace.commands += 1;
    if (event.level === 'error') trace.errors += 1;
  }
  return [...map.values()].map((trace) => ({ ...trace, creditsComplete: [...trace.costAttempts.values()].every(Boolean) })).reverse();
}

export const PHASES = [
  { key: 'wait', label: 'ожидание', title: 'До первого события upstream' },
  { key: 'think', label: 'мышление', title: 'Сигнал thinking от upstream' },
  { key: 'tools', label: 'аргументы инструментов', title: 'Генерация аргументов инструментов' },
  { key: 'out', label: 'ответ', title: 'Вывод ответа' },
  { key: 'other', label: 'без детализации', title: 'Длительность без подтверждённой фазы' },
];

/** Phase lengths in ms: waiting, thinking, tool arguments, output. */
export function phasesOf(trace, now = Date.now() / 1000) {
  const open = trace.status === 'running';
  const end = open ? Math.max(now, trace.last) : trace.ended ?? trace.last;
  const duration = Number.isFinite(trace.duration) ? trace.duration : Math.max(0, (end - trace.started) * 1000);
  const rawThink = Math.max(0, trace.thinking || 0) + (trace.thinkingStarted !== null ? Math.max(0, (end - trace.thinkingStarted) * 1000) : 0);
  const wait = Math.min(duration, Math.max(0, trace.firstSignal !== null ? (trace.firstSignal - trace.started) * 1000
    : Number(trace.ttft) || (rawThink || trace.args ? 0 : duration)));
  const think = Math.min(duration - wait, rawThink);
  const tools = Math.min(duration - wait - think, Math.max(0, trace.args || 0)
    + (trace.toolStarted !== null ? Math.max(0, (end - trace.toolStarted) * 1000) : 0));
  const known = wait + think + tools;
  const remaining = Math.max(0, duration - known);
  const out = Math.min(remaining, trace.textMs + (trace.textStarted !== null ? Math.max(0, (end - trace.textStarted) * 1000) : 0));
  const other = remaining - out;
  const total = duration;
  return { wait, think, tools, out, other, total, open };
}
