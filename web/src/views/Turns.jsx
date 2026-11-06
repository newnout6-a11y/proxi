import { useEffect, useMemo, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { ChevronRight } from 'lucide-react';
import { Section, ease } from '../motion.jsx';
import { Empty, Tag } from '../ui.jsx';
import { TapeLegend, TurnTape } from '../charts.jsx';
import { LEVELS, STATUS_TONE, TURN_STATUS } from '../catalog.js';
import { buildTraces, phasesOf } from '../turns.js';
import { fmtAgo, fmtClock, fmtFixed, fmtInt, fmtMs, fmtTokens } from '../format.js';
import { TurnsArt } from '../stage/Art.jsx';

function Steps({ trace }) {
  const start = trace.started;
  return (
    <motion.ol className="steps" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
      transition={{ duration: 0.15, ease }}>
      {trace.events.map((event) => (
        <li key={event.seq} className={`step level-${event.level}`}>
          <span className="step-time">+{fmtMs((event.ts - start) * 1000)}</span>
          <span className="step-kind">{event.kind}</span>
          <span className="step-msg">{event.message}</span>
          <span className={`lvl level-${event.level}`}>{LEVELS[event.level]?.label}</span>
        </li>
      ))}
    </motion.ol>
  );
}

export default function Turns({ journal, overview, embedded }) {
  const traces = useMemo(() => buildTraces(journal.events), [journal.events]);
  const [open, setOpen] = useState(null);
  const [now, setNow] = useState(() => Date.now() / 1000);
  const running = traces.some((trace) => trace.status === 'running');
  useEffect(() => {
    if (!running) return undefined;
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, [running]);
  const list = traces.slice(0, 120);
  const max = Math.max(1, ...list.map((trace) => phasesOf(trace, now).total));

  return (
    <Section embedded={embedded} className="turns">
      <header className="page-head">
        <div>
          {!embedded && <h1>Ходы моделей</h1>}
          <p className="page-meta">{fmtInt(traces.length)} в буфере · длина ленты в общем масштабе</p>
        </div>
        <TapeLegend />
      </header>
      <section className="panel trace-panel">
        {list.length > 0 && (
          <div className="trace-cols" aria-hidden="true">
            <span>статус</span><span>модель</span><span>лента хода</span><span>первый токен</span><span>кэш</span><span>кредиты</span><span>когда</span>
          </div>
        )}
        {list.map((trace) => {
          const isOpen = open === trace.id;
          const phases = phasesOf(trace, now);
          const totalIn = (trace.inputTokens || 0) + (trace.cacheRead || 0) + (trace.cacheWrite || 0);
          const hit = totalIn > 0 ? trace.cacheRead / totalIn : null;
          return (
            <article key={trace.id} className={`trace status-${trace.status} ${isOpen ? 'open' : ''}`}>
              <button type="button" className="trace-head" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : trace.id)}>
                <span className="trace-status">
                  <ChevronRight className="chev" size={16} strokeWidth={1.5} aria-hidden="true" />
                  <Tag tone={STATUS_TONE[trace.status] || 'muted'}>{TURN_STATUS[trace.status] || trace.status}</Tag>
                </span>
                <span className="trace-model">
                  <strong>{trace.model || '—'}</strong>
                  <small>{[trace.effort && `effort ${trace.effort}`, trace.transport, trace.dialect].filter(Boolean).join(' · ')}</small>
                </span>
                <span className="trace-tape">
                  <TurnTape trace={trace} max={max} now={now} />
                  <span className="trace-phases">
                    <span title="Время до первого токена">{fmtMs(trace.ttft)}</span>
                    {phases.think > 0 && <span title="По событиям thinking от upstream">мышление {fmtMs(phases.think)}</span>}
                    {trace.status === 'running' && <span className="live-phase">{trace.phase === 'think' ? 'upstream: thinking' : trace.phase === 'out' ? 'пишет ответ' : trace.phase === 'tools' ? 'аргументы инструмента' : trace.phase === 'wait' ? 'ждём upstream' : 'ожидаем событие'}</span>}
                    {trace.args > 0 && <span title="Генерация аргументов инструментов">инструменты {fmtMs(trace.args)}</span>}
                    <span>всего {fmtMs(phases.total)}</span>
                  </span>
                </span>
                <span className="trace-num">{fmtMs(trace.ttft)}</span>
                <span className="trace-num">{fmtTokens(trace.cacheRead)}{hit !== null && <small> · {Math.round(hit * 100)}%</small>}</span>
                <span className="trace-num trace-cost" title={trace.credits === null ? 'Стоимость не сообщена' : !trace.creditsComplete ? 'Часть стоимости не сообщена' : 'Сообщённая стоимость'}>
                  {trace.credits !== null && !trace.creditsComplete ? '≥ ' : ''}{fmtFixed(trace.credits, 2)}
                  <small>{trace.credits === null ? 'нет данных' : !trace.creditsComplete ? 'неполные данные' : ''}</small>
                </span>
                <time className="trace-when" title={fmtClock(trace.started)}>
                  {fmtAgo(trace.started)}
                  {(trace.tools > 0 || trace.commands > 0 || trace.errors > 0) && (
                    <small>
                      {[trace.tools && `инстр. ${trace.tools}`, trace.commands && `команд ${trace.commands}`, trace.errors && `ошибок ${trace.errors}`]
                        .filter(Boolean).join(' · ')}
                    </small>
                  )}
                </time>
              </button>
              <AnimatePresence>{isOpen && <Steps trace={trace} />}</AnimatePresence>
            </article>
          );
        })}
        {!list.length && (
          <Empty art={<TurnsArt />} title="Ходов пока нет"
            text="Каждый запрос к модели появится здесь лентой фаз: ожидание, мышление, инструменты, ответ"
            hint={overview?.connection?.anthropic_base_url || 'http://127.0.0.1:8787'} />
        )}
      </section>
    </Section>
  );
}
