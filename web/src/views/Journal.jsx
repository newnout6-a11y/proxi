import { useEffect, useMemo, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { ArrowDown, ChevronRight, Download, Pause, Play, Search, Trash, X } from 'lucide-react';
import { api } from '../api.js';
import { Dot, Section, ease } from '../motion.jsx';
import { Button, CopyButton, Empty, Tag, useToast } from '../ui.jsx';
import { CATEGORIES, LEVELS, category } from '../catalog.js';
import { fmtClock, fmtDateTime, fmtInt } from '../format.js';
import { JournalArt } from '../stage/Art.jsx';

const PAGE = 400;
const LEVEL_TONE = { success: 'pos', error: 'neg', warn: 'warn' };

function Detail({ event }) {
  const json = JSON.stringify(event, null, 2);
  const entries = Object.entries(event.data || {}).filter(([, value]) => value !== null && value !== '' && value !== undefined);
  return (
    <motion.div className="event-detail" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
      transition={{ duration: 0.15, ease }}>
      <dl className="detail-grid">
        <div><dt>время</dt><dd>{fmtDateTime(event.ts)}</dd></div>
        <div><dt>тип</dt><dd>{event.kind}</dd></div>
        {event.turn_id && <div><dt>ход</dt><dd>{event.turn_id}</dd></div>}
        {event.request_id && <div><dt>запрос</dt><dd>{event.request_id}</dd></div>}
        {event.conversation && <div><dt>диалог</dt><dd>{event.conversation}</dd></div>}
        {entries.map(([key, value]) => (
          <div key={key}>
            <dt>{key}</dt>
            <dd>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</dd>
          </div>
        ))}
      </dl>
      <div className="detail-json">
        <div className="detail-json-head"><span>JSON</span><CopyButton value={json} label="Копировать событие" /></div>
        <pre>{json}</pre>
      </div>
    </motion.div>
  );
}

export default function Journal({ apiKey, journal, overview, onUnauthorized, embedded }) {
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
    if (follow && listRef.current) listRef.current.scrollTo({ top: 0 });
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
    // The server empties the memory AND deletes journal.jsonl with every rotated journal.jsonl.N (admin.py, journal.reset):
    // the dialog used to promise the file was kept, and hours of evidence went with one click.
    if (!window.confirm('Очистить журнал? Будут стёрты события в памяти relay и файлы на диске: journal.jsonl и все journal.jsonl.N. Восстановить их будет нельзя.')) return;
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

  const live = journal.status === 'live';
  const filtering = levels.size > 0 || cats.size > 0 || query;

  return (
    <Section embedded={embedded} className="journal">
      <header className="page-head">
        <div>
          {!embedded && <h1>Журнал событий</h1>}
          <p className="page-meta">в буфере {fmtInt(journal.events.length)} · показано {fmtInt(shown.length)} из {fmtInt(filtered.length)}</p>
        </div>
        <div className="head-actions">
          <span className="status">
            <Dot tone={live && !journal.paused ? 'pos' : 'warn'} live={live && !journal.paused} />
            {journal.paused ? 'на паузе' : live ? 'live' : 'переподключение'}
          </span>
          <Button icon={journal.paused ? Play : Pause} tone={journal.paused ? 'primary' : 'default'}
            onClick={() => journal.setPaused(!journal.paused)}>
            {journal.paused ? `Продолжить${journal.pending ? ` (+${journal.pending})` : ''}` : 'Пауза'}
          </Button>
          <Button icon={Download} onClick={exportJsonl} disabled={!filtered.length}>Экспорт</Button>
          <Button icon={Trash} tone="danger" onClick={clearAll}>Очистить</Button>
        </div>
      </header>

      <section className="panel filters">
        <div className="search">
          <Search size={16} strokeWidth={1.5} aria-hidden="true" />
          <input aria-label="Поиск по журналу" placeholder="Поиск" value={query} onChange={(event) => setQuery(event.target.value)} />
          {query && (
            <button type="button" className="icon-button ghost" aria-label="Очистить поиск" onClick={() => setQuery('')}>
              <X size={16} strokeWidth={1.5} />
            </button>
          )}
        </div>
        <div className="chips" role="group" aria-label="Уровни">
          {Object.entries(LEVELS).map(([id, info]) => (
            <button key={id} type="button" className={`chip level-${id} ${levels.has(id) ? 'on' : ''}`} aria-pressed={levels.has(id)}
              onClick={() => toggle(setLevels)(id)}>
              <Dot tone={LEVEL_TONE[id] || 'muted'} />{info.label}<span className="chip-count num">{fmtInt(counts.level[id] || 0)}</span>
            </button>
          ))}
        </div>
        <div className="chips" role="group" aria-label="Категории">
          {Object.entries(CATEGORIES).map(([id, info]) => (
            <button key={id} type="button" className={`chip ${cats.has(id) ? 'on' : ''}`} aria-pressed={cats.has(id)}
              onClick={() => toggle(setCats)(id)}>
              {info.label}<span className="chip-count num">{fmtInt(counts.category[id] || 0)}</span>
            </button>
          ))}
          {filtering && (
            <button type="button" className="chip reset" onClick={() => { setLevels(new Set()); setCats(new Set()); setQuery(''); }}>
              Сбросить фильтры
            </button>
          )}
        </div>
      </section>

      <section className="panel log-panel">
        <div className="log-head">
          <span className="log-cols" aria-hidden="true"><span>время</span><span>уровень</span><span>категория</span><span>сообщение</span></span>
          <label className="check">
            <input type="checkbox" checked={follow} onChange={(event) => setFollow(event.target.checked)} />
            <span>Следить за новыми</span>
          </label>
        </div>
        <div className="log" ref={listRef} role="log" aria-label="События журнала">
          {journal.paused && journal.pending > 0 && (
            <button type="button" className="pending-bar" onClick={() => journal.setPaused(false)}>
              <ArrowDown size={16} strokeWidth={1.5} /> {fmtInt(journal.pending)} новых событий, показать
            </button>
          )}
          {shown.map((event) => {
            const info = category(event.category);
            const isOpen = open === event.seq;
            return (
              <div key={event.seq} className={`log-row level-${event.level} ${isOpen ? 'open' : ''}`}>
                <button type="button" className="log-line" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : event.seq)}>
                  <ChevronRight className="chev" size={16} strokeWidth={1.5} aria-hidden="true" />
                  <time>{fmtClock(event.ts)}</time>
                  <span className={`lvl level-${event.level}`}>{LEVELS[event.level]?.label || event.level}</span>
                  <span className="cat">{info.label}</span>
                  <span className="msg">{event.message}</span>
                  {event.turn_id && <span className="turn-tag" title={`ход ${event.turn_id}`}>{event.turn_id.slice(0, 6)}</span>}
                  {event.restored && <Tag title="Восстановлено из journal.jsonl после перезапуска">из файла</Tag>}
                  <span className="seq">#{event.seq}</span>
                </button>
                <AnimatePresence>{isOpen && <Detail event={event} />}</AnimatePresence>
              </div>
            );
          })}
          {!shown.length && (journal.events.length
            ? <Empty title="Ничего не найдено" text="Измените фильтры или запрос" />
            : <Empty art={<JournalArt />} title="Журнал пуст"
                text="Каждый запрос IDE к relay появится здесь строкой с моделью, статусом и временем"
                hint={overview?.connection?.anthropic_base_url || 'http://127.0.0.1:8787'} />)}
          {filtered.length > limit && (
            <button type="button" className="button wide" onClick={() => setLimit((n) => n + PAGE)}>
              Показать ещё {fmtInt(Math.min(PAGE, filtered.length - limit))}
            </button>
          )}
        </div>
      </section>
    </Section>
  );
}
