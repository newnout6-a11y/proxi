import { useEffect, useMemo, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { Button, Empty, Tag } from '../ui.jsx';
import { fmtInt } from '../format.js';
import { Select } from './controls.jsx';
import { diffLines } from './diff.js';
import DiffView from './DiffView.jsx';
import HeadView from './HeadView.jsx';
import { PreviewLists } from './PreviewResult.jsx';
import { utf8Bytes } from './template.js';

const clock = (ts) => (Number.isFinite(ts) ? new Date(ts * 1000).toLocaleTimeString('ru-RU', { hour12: false }) : '—');

function Entry({ item, selected, onSelect }) {
  return (
    <li>
      <button type="button" className={`seen-item${selected ? ' on' : ''}`} aria-current={selected ? 'true' : undefined} onClick={onSelect}>
        <span className="seen-time mono">{clock(item.ts)}</span>
        <span className="seen-main">
          <strong className="mono">{item.model || 'модель?'}</strong>
          <small>{[item.client, item.agent, item.purpose].filter(Boolean).join(' · ')}</small>
        </span>
        <span className="seen-route">
          <span className="mono">{item.profile === null ? 'legacy' : `${item.rule || 'по умолчанию'} → ${item.profile}`}</span>
          <span className="seen-tags">
            {item.rev !== null && <Tag>{`ревизия ${item.rev}`}</Tag>}
            {item.pinned && <Tag tone="accent" title="Диалог остаётся на своей ревизии">закреплён</Tag>}
          </span>
        </span>
      </button>
    </li>
  );
}

/** The last requests with the head the model really got, and the difference between two of them. */
export default function SeenTab({ items, error, loading, refresh, onPreview }) {
  const [selected, setSelected] = useState(null);
  const [base, setBase] = useState('prev');
  const [view, setView] = useState('head');
  const index = Math.max(0, items.findIndex((item) => item.request_id === selected));
  const item = items[index] || null;
  // Newest first, so the neighbour that came before is the next one down
  const previous = base === 'prev' ? items[index + 1] : items.find((other) => other.request_id === base);
  useEffect(() => { if (selected === null && items[0]) setSelected(items[0].request_id); }, [items, selected]);
  const rows = useMemo(() => (item && previous ? diffLines(previous.head, item.head) : []), [item, previous]);

  return (
    <div className="p-seen">
      <section className="panel seen-list-panel" aria-label="Последние запросы">
        <div className="panel-head">
          <div>
            <h2>Последние запросы</h2>
            <p className="panel-meta">{fmtInt(items.length)} в памяти relay: тексты не пишутся в журнал и пропадают при перезапуске</p>
          </div>
          <Button icon={RefreshCw} onClick={refresh} disabled={loading}>Обновить</Button>
        </div>
        {error && <p className="banner tone-neg" role="alert">Не удалось загрузить: {error}</p>}
        {!items.length && !error && (
          <Empty title="Запросов пока нет" text="Здесь появятся запросы, для которых сработал профиль из документа: с полным head, который увидела модель" />
        )}
        {items.length > 0 && (
          <ul className="seen-list">
            {items.map((entry) => <Entry key={entry.request_id || entry.ts} item={entry} selected={entry === item} onSelect={() => { setSelected(entry.request_id); setBase('prev'); }} />)}
          </ul>
        )}
      </section>
      {item && (
        <section className="panel seen-detail" aria-label="Выбранный запрос">
          <div className="panel-head">
            <div>
              <h2>Что увидела модель</h2>
              <p className="panel-meta mono">{item.request_id || '—'} · {clock(item.ts)}</p>
            </div>
            {item.has_request && <Button onClick={() => onPreview(item)}>Проверить в предпросмотре</Button>}
          </div>
          <dl className="p-facts">
            {[['Клиент', item.client || '—'], ['Модель', item.model || '—'], ['Назначение', item.purpose || '—'],
              ['Профиль', item.profile === null ? 'legacy' : item.profile], ['Правило', item.rule || '—'],
              ['Ревизия', item.rev === null ? '—' : String(item.rev)], ['Размер head', `${fmtInt(utf8Bytes(item.head))} байт`]]
              .map(([name, value]) => <div key={name}><dt>{name}</dt><dd className="mono">{value}</dd></div>)}
          </dl>
          <div className="segmented" role="group" aria-label="Что показать">
            <button type="button" aria-pressed={view === 'head'} onClick={() => setView('head')}>Полный head</button>
            <button type="button" aria-pressed={view === 'diff'} onClick={() => setView('diff')}>Сравнить с другим</button>
          </div>
          {view === 'head' ? (
            <HeadView head={item.head} parts={item.parts} tall />
          ) : (
            <>
              <Select label="Сравнить с" value={base} onChange={setBase}
                options={[{ value: 'prev', label: items[index + 1] ? `Предыдущий: ${clock(items[index + 1].ts)} · ${items[index + 1].model}` : 'Предыдущего нет' },
                  ...items.filter((other) => other !== item).map((other) => ({ value: other.request_id, label: `${clock(other.ts)} · ${other.model || 'модель?'} · ${other.profile ?? 'legacy'}` }))]} />
              {previous ? (
                <DiffView rows={rows} label="Различия между двумя head" emptyText="Один из head пуст"
                  unchangedText="Head не изменился между этими запросами: тот же текст, тот же кэш" />
              ) : (
                <p className="muted small p-empty-line">Не с чем сравнивать: выберите другой запрос</p>
              )}
            </>
          )}
          <PreviewLists data={{ warnings: [], edits_unmatched: [], history_changes: item.history_changes, tools_changes: item.tools_changes }} />
        </section>
      )}
    </div>
  );
}
