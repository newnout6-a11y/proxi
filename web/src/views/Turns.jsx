import { useMemo, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Route, Wrench, Terminal, Coins, Timer, Database, TriangleAlert } from 'lucide-react';
import { Page, Reveal, spring } from '../motion.jsx';
import { Badge, Empty } from '../ui.jsx';
import { LEVELS, TURN_STATUS, category } from '../catalog.js';
import { fmtAgo, fmtClock, fmtMs, fmtNum, fmtTokens } from '../format.js';

const STATUS_TONE = { running: 'info', stop: 'success', tool_calls: 'accent', cancelled: 'warn', timeout: 'warn', error: 'danger' };

/** Build traces client-side from the live journal so they update in real time. */
function buildTraces(events) {
  const map = new Map();
  for (const event of events) {
    if (!event.turn_id) continue;
    let trace = map.get(event.turn_id);
    if (!trace) {
      trace = { id: event.turn_id, started: event.ts, ended: null, status: 'running', model: null, effort: null,
        transport: null, dialect: null, ttft: null, duration: null, credits: 0, cacheRead: 0, cacheWrite: 0,
        tools: 0, commands: 0, errors: 0, events: [] };
      map.set(event.turn_id, trace);
    }
    const data = event.data || {};
    trace.events.push(event);
    if (event.kind === 'turn.begin') Object.assign(trace, { model: data.model, effort: data.effort, transport: data.transport, dialect: data.dialect });
    if (event.kind === 'turn.end') Object.assign(trace, { ended: event.ts, duration: data.duration_ms, status: data.reason || 'stop' });
    if (event.kind === 'upstream.first_token') trace.ttft = data.ttft_ms;
    if (event.kind === 'upstream.usage') {
      trace.credits += Number(data.credits) || 0;
      trace.cacheRead += Number(data.cache_read) || 0;
      trace.cacheWrite += Number(data.cache_write) || 0;
    }
    if (event.kind === 'tool.call') trace.tools += 1;
    if (event.kind === 'exec.result') trace.commands += 1;
    if (event.level === 'error') trace.errors += 1;
  }
  return [...map.values()].reverse();
}

function Timeline({ trace }) {
  const start = trace.started;
  const end = trace.ended || trace.events[trace.events.length - 1].ts;
  const span = Math.max(0.001, end - start);
  return (
    <motion.div className="timeline" initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}>
      <div className="timeline-track">
        {trace.events.map((event, index) => (
          <motion.span key={event.seq} className={`timeline-dot level-${event.level}`}
            style={{ left: `${((event.ts - start) / span) * 100}%` }}
            initial={{ scale: 0 }} animate={{ scale: 1 }} transition={{ ...spring, delay: index * 0.035 }}
            title={`${event.kind} · +${fmtMs((event.ts - start) * 1000)}`} />
        ))}
        <motion.span className="timeline-bar" initial={{ scaleX: 0 }} animate={{ scaleX: 1 }} transition={{ duration: 0.9, ease: [0.22, 1, 0.36, 1] }} />
      </div>
      <ol className="steps">
        {trace.events.map((event, index) => {
          const Icon = category(event.category).icon;
          return (
            <motion.li key={event.seq} className={`step level-${event.level}`}
              initial={{ opacity: 0, x: -14 }} animate={{ opacity: 1, x: 0 }} transition={{ ...spring, delay: index * 0.03 }}>
              <span className="step-time mono">+{fmtMs((event.ts - start) * 1000)}</span>
              <span className="step-icon"><Icon size={13} /></span>
              <span className="step-msg">{event.message}</span>
              <span className={`lvl level-${event.level}`}>{LEVELS[event.level]?.label}</span>
            </motion.li>
          );
        })}
      </ol>
    </motion.div>
  );
}

export default function Turns({ journal }) {
  const traces = useMemo(() => buildTraces(journal.events), [journal.events]);
  const [open, setOpen] = useState(null);

  return (
    <Page className="turns">
      <Reveal as="header" className="page-head">
        <div>
          <p className="eyebrow">Трассы · ход за ходом</p>
          <h1>Ходы моделей</h1>
        </div>
        <Badge>{traces.length} ходов в буфере</Badge>
      </Reveal>
      <div className="trace-list">
        <AnimatePresence initial={false}>
          {traces.slice(0, 120).map((trace) => {
            const isOpen = open === trace.id;
            const hit = trace.cacheRead + trace.cacheWrite ? trace.cacheRead / (trace.cacheRead + trace.cacheWrite) : null;
            return (
              <motion.article key={trace.id} layout className={`trace status-${trace.status} ${isOpen ? 'open' : ''}`}
                initial={{ opacity: 0, y: -16, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0 }} transition={spring}>
                <button type="button" className="trace-head" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : trace.id)}>
                  <span className="trace-status">
                    {trace.status === 'running'
                      ? <motion.span className="spinner small" animate={{ rotate: 360 }} transition={{ duration: 0.9, repeat: Infinity, ease: 'linear' }} />
                      : <Badge tone={STATUS_TONE[trace.status] || 'neutral'}>{TURN_STATUS[trace.status] || trace.status}</Badge>}
                  </span>
                  <span className="trace-model">
                    <strong>{trace.model || '—'}</strong>
                    <small className="muted">{[trace.effort && `effort ${trace.effort}`, trace.transport, trace.dialect].filter(Boolean).join(' · ')}</small>
                  </span>
                  <span className="trace-metrics">
                    <span title="Время до первого токена"><Timer size={13} />{fmtMs(trace.ttft)}</span>
                    <span title="Длительность хода"><Route size={13} />{fmtMs(trace.duration)}</span>
                    <span title="Кэш: прочитано"><Database size={13} />{fmtTokens(trace.cacheRead)}{hit !== null && <em>{Math.round(hit * 100)}%</em>}</span>
                    <span title="Кредиты"><Coins size={13} />{fmtNum(trace.credits, 4)}</span>
                    {trace.tools > 0 && <span title="Вызовы инструментов"><Wrench size={13} />{trace.tools}</span>}
                    {trace.commands > 0 && <span title="Локальные команды"><Terminal size={13} />{trace.commands}</span>}
                    {trace.errors > 0 && <span className="danger-text" title="Ошибки"><TriangleAlert size={13} />{trace.errors}</span>}
                  </span>
                  <time className="muted small" title={fmtClock(trace.started)}>{fmtAgo(trace.started)}</time>
                </button>
                <AnimatePresence>{isOpen && <Timeline trace={trace} />}</AnimatePresence>
              </motion.article>
            );
          })}
        </AnimatePresence>
        {!traces.length && <Empty icon={Route} title="Ходов пока нет" text="Каждый запрос к модели появится здесь с таймлайном шагов." />}
      </div>
    </Page>
  );
}
