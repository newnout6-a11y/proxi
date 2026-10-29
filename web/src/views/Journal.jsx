import { useEffect, useMemo, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { ArrowDown, ChevronRight, Download, Pause, Play, Search, Trash, X } from 'lucide-react';
import { api } from '../api.js';
import { Page, PulseDot, Reveal, spring } from '../motion.jsx';
import { Badge, Button, CopyButton, Empty, useToast } from '../ui.jsx';
import { CATEGORIES, LEVELS, category } from '../catalog.js';
import { fmtClock, fmtDateTime, fmtInt } from '../format.js';

const PAGE = 400;

function Detail({ event }) {
  const json = JSON.stringify(event, null, 2);
  const entries = Object.entries(event.data || {}).filter(([, value]) => value !== null && value !== '' && value !== undefined);
  return (
    <motion.div className="event-detail" initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }}
      exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.22 }}>
      <div className="detail-inner">
        <dl className="detail-grid">
          <div><dt>время</dt><dd className="mono">{fmtDateTime(event.ts)}</dd></div>
          <div><dt>тип</dt><dd className="mono">{event.kind}</dd></div>
          {event.turn_id && <div><dt>ход</dt><dd className="mono">{event.turn_id}</dd></div>}
          {event.request_id && <div><dt>запрос</dt><dd className="mono">{event.request_id}</dd></div>}
          {event.conversation && <div><dt>диалог</dt><dd className="mono">{event.conversation}</dd></div>}
          {entries.map(([key, value]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd className="mono">{typeof value === 'object' ? JSON.stringify(value) : String(value)}</dd>
            </div>
          ))}
        </dl>
        <div className="detail-json">
          <div className="detail-json-head"><span className="muted small">JSON</span><CopyButton value={json} label="Копировать событие" /></div>
          <pre>{json}</pre>
        </div>
      </div>
    </motion.div>
  );
}

export default function Journal({ apiKey, journal, onUnauthorized }) {
  const toast = useToast();
  const [levels, setLevels] = useState(() => new Set());
  const [cats, setCats] = useState(() => new Set());
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(null);
  const [limit, setLimit] = useState(PAGE);
  const [follow, setFollow] = useState(true);
  const listRef = useRef(null);

  const needle = query.trim().toLowerCase();
  const filtered = useMemo(() => journal.events.filter((event) => (
    (!levels.size || levels.has(event.level))
    && (!cats.size || cats.has(event.category))
    && (!needle || `${event.message} ${event.kind} ${event.turn_id || ''} ${event.request_id || ''} ${JSON.stringify(event.data)}`.toLowerCase().includes(needle))
  )), [journal.events, levels, cats, needle]);
  const shown = filtered.slice(-limit).reverse();

  const counts = useMemo(() => {
    const result = { level: {}, category: {} };
    for (const event of journal.events) {
      result.level[event.level] = (result.level[event.level] || 0) + 1;
      result.category[event.category] = (result.category[event.category] || 0) + 1;
    }
    return result;
  }, [journal.events]);

  useEffect(() => {
    if (follow && listRef.current) listRef.current.scrollTo({ top: 0, behavior: 'smooth' });
  }, [shown[0]?.seq, follow]);

  const toggle = (setter) => (value) => setter((previous) => {
    const next = new Set(previous);
    if (next.has(value)) next.delete(value); else next.add(value);
    return next;
  });

  function exportJsonl() {
    const blob = new Blob(filtered.map((event) => `${JSON.stringify(event)}\n`), { type: 'application/x-ndjson' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `relay-journal-${new Date().toISOString().replace(/[:.]/g, '-')}.jsonl`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    toast({ tone: 'success', title: 'Экспорт готов', text: `${fmtInt(filtered.length)} событий в JSONL` });
  }

  async function clearAll() {
    if (!window.confirm('Очистить журнал в памяти relay? Файл journal.jsonl на диске сохранится.')) return;
    try {
      await api(apiKey, 'events/clear', { method: 'POST', body: {} });
      journal.reset();
      setOpen(null);
      toast({ tone: 'warn', title: 'Журнал очищен' });
    } catch (failure) {
      if (failure.status === 401) onUnauthorized();
      else toast({ tone: 'error', title: 'Не удалось очистить', text: failure.message });
    }
  }

  return (
    <Page className="journal">
      <Reveal as="header" className="page-head">
        <div>
          <p className="eyebrow">Журнал · каждое действие relay</p>
          <h1>Журнал событий</h1>
        </div>
        <div className="head-actions">
          <Badge tone={journal.status === 'live' ? 'success' : 'warn'}>
            <PulseDot tone={journal.status === 'live' ? 'success' : 'warn'} live={journal.status === 'live' && !journal.paused} />
            {journal.paused ? 'на паузе' : journal.status === 'live' ? 'live' : 'переподключение'}
          </Badge>
          <Button icon={journal.paused ? Play : Pause} tone={journal.paused ? 'primary' : 'default'}
            onClick={() => journal.setPaused(!journal.paused)}>
            {journal.paused ? `Продолжить${journal.pending ? ` (+${journal.pending})` : ''}` : 'Пауза'}
          </Button>
          <Button icon={Download} onClick={exportJsonl} disabled={!filtered.length}>Экспорт</Button>
          <Button icon={Trash} tone="danger" onClick={clearAll}>Очистить</Button>
        </div>
      </Reveal>

      <Reveal className="panel filters">
        <div className="search">
          <Search size={16} aria-hidden="true" />
          <input aria-label="Поиск по журналу" placeholder="Поиск: сообщение, тип, ход, команда, модель…"
            value={query} onChange={(event) => setQuery(event.target.value)} />
          {query && <button type="button" className="icon-button ghost" aria-label="Очистить поиск" onClick={() => setQuery('')}><X size={14} /></button>}
        </div>
        <div className="chips" role="group" aria-label="Уровни">
          {Object.entries(LEVELS).map(([id, info]) => (
            <motion.button key={id} type="button" whileTap={{ scale: 0.92 }}
              className={`chip level-${id} ${levels.has(id) ? 'on' : ''}`} aria-pressed={levels.has(id)}
              onClick={() => toggle(setLevels)(id)}>
              <i />{info.label}<b className="num">{fmtInt(counts.level[id] || 0)}</b>
            </motion.button>
          ))}
        </div>
        <div className="chips" role="group" aria-label="Категории">
          {Object.entries(CATEGORIES).map(([id, info]) => {
            const Icon = info.icon;
            return (
              <motion.button key={id} type="button" whileTap={{ scale: 0.92 }}
                className={`chip ${cats.has(id) ? 'on' : ''}`} aria-pressed={cats.has(id)}
                onClick={() => toggle(setCats)(id)}>
                <Icon size={13} />{info.label}<b className="num">{fmtInt(counts.category[id] || 0)}</b>
              </motion.button>
            );
          })}
          {(levels.size > 0 || cats.size > 0 || query) && (
            <button type="button" className="chip reset" onClick={() => { setLevels(new Set()); setCats(new Set()); setQuery(''); }}>
              Сбросить фильтры
            </button>
          )}
        </div>
      </Reveal>

      <Reveal className="panel log-panel">
        <div className="log-head">
          <span className="muted small">Показано {fmtInt(shown.length)} из {fmtInt(filtered.length)} · в буфере {fmtInt(journal.events.length)}</span>
          <label className="check small">
            <input type="checkbox" checked={follow} onChange={(event) => setFollow(event.target.checked)} />
            <span>Следить за новыми</span>
          </label>
        </div>
        <div className="log" ref={listRef} role="log" aria-label="События журнала">
          <AnimatePresence initial={false}>
            {journal.paused && journal.pending > 0 && (
              <motion.button key="pending" type="button" className="pending-bar" initial={{ opacity: 0, y: -10 }}
                animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }} onClick={() => journal.setPaused(false)}>
                <ArrowDown size={14} /> {fmtInt(journal.pending)} новых событий — показать
              </motion.button>
            )}
            {shown.map((event) => {
              const info = category(event.category);
              const Icon = info.icon;
              const isOpen = open === event.seq;
              return (
                <motion.div key={event.seq} className={`log-row level-${event.level} ${isOpen ? 'open' : ''}`}
                  initial={{ opacity: 0, x: -30, height: 0, backgroundColor: 'var(--accent-soft)' }}
                  animate={{ opacity: 1, x: 0, height: 'auto', backgroundColor: 'rgba(0,0,0,0)' }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ ...spring, height: { duration: 0.22 }, backgroundColor: { duration: 1.4 } }}>
                  <button type="button" className="log-line" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : event.seq)}>
                    <motion.span className="chev" animate={{ rotate: isOpen ? 90 : 0 }}><ChevronRight size={14} /></motion.span>
                    <time className="mono">{fmtClock(event.ts)}</time>
                    <span className={`lvl level-${event.level}`}>{LEVELS[event.level]?.label || event.level}</span>
                    <span className="cat"><Icon size={13} />{info.label}</span>
                    <span className="msg">{event.message}</span>
                    {event.turn_id && <span className="turn-tag mono" title={`ход ${event.turn_id}`}>{event.turn_id.slice(0, 6)}</span>}
                    {event.restored && <span className="restored" title="Восстановлено из journal.jsonl после перезапуска">↺</span>}
                    <span className="seq mono">#{event.seq}</span>
                  </button>
                  <AnimatePresence>{isOpen && <Detail event={event} />}</AnimatePresence>
                </motion.div>
              );
            })}
          </AnimatePresence>
          {!shown.length && (
            <Empty icon={Search} title={journal.events.length ? 'Ничего не найдено' : 'Журнал пуст'}
              text={journal.events.length ? 'Измените фильтры или запрос.' : 'Новые события появятся здесь в реальном времени.'} />
          )}
          {filtered.length > limit && (
            <button type="button" className="button wide" onClick={() => setLimit((n) => n + PAGE)}>
              Показать ещё {fmtInt(Math.min(PAGE, filtered.length - limit))}
            </button>
          )}
        </div>
      </Reveal>
    </Page>
  );
}
