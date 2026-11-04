/** Build traces client-side from the live journal so they update in real time. */
export function buildTraces(events) {
  const map = new Map();
  for (const event of events) {
    if (!event.turn_id) continue;
    let trace = map.get(event.turn_id);
    if (!trace) {
      trace = { id: event.turn_id, started: event.ts, ended: null, status: 'running', model: null, effort: null,
        transport: null, dialect: null, ttft: null, duration: null, credits: 0, inputTokens: 0, cacheRead: 0, cacheWrite: 0,
        tools: 0, commands: 0, errors: 0, thinking: 0, args: 0, last: event.ts, events: [] };
      map.set(event.turn_id, trace);
    }
    const data = event.data || {};
    trace.events.push(event);
    trace.last = Math.max(trace.last, event.ts);
    if (event.kind === 'turn.begin') Object.assign(trace, { model: data.model, effort: data.effort, transport: data.transport, dialect: data.dialect });
    if (event.kind === 'turn.end') Object.assign(trace, { ended: event.ts, duration: data.duration_ms, status: data.reason || 'stop' });
    if (event.kind === 'upstream.first_token') trace.ttft = data.ttft_ms;
    if (event.kind === 'upstream.usage') {
      trace.credits += Number(data.credits) || 0;
      trace.inputTokens += Number(data.input_tokens) || 0;
      trace.cacheRead += Number(data.cache_read) || 0;
      trace.cacheWrite += Number(data.cache_write) || 0;
    }
    if (event.kind === 'upstream.thinking_end') trace.thinking += Number(data.thinking_ms) || 0;
    if (event.kind === 'tool.call') {
      trace.tools += 1;
      trace.args += Number(data.args_ms) || 0;
    }
    if (event.kind === 'exec.result') trace.commands += 1;
    if (event.level === 'error') trace.errors += 1;
  }
  return [...map.values()].reverse();
}

export const PHASES = [
  { key: 'wait', label: 'до первого токена', title: 'Время до первого токена' },
  { key: 'think', label: 'мышление', title: 'Скрытое мышление' },
  { key: 'tools', label: 'аргументы инструментов', title: 'Генерация аргументов инструментов' },
  { key: 'out', label: 'ответ', title: 'Вывод ответа' },
];

/** Phase lengths in ms: waiting, thinking, tool arguments, output. */
export function phasesOf(trace) {
  const wait = Math.max(0, Number(trace.ttft) || 0);
  const think = Math.max(0, trace.thinking || 0);
  const tools = Math.max(0, trace.args || 0);
  const known = wait + think + tools;
  const duration = Number.isFinite(trace.duration) ? trace.duration : Math.max(0, (trace.last - trace.started) * 1000);
  const out = Math.max(0, duration - known);
  const total = Math.max(duration, known);
  return { wait, think, tools, out, total, open: trace.status === 'running' };
}
