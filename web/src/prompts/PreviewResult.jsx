import { TriangleAlert } from 'lucide-react';
import { fmtInt } from '../format.js';
import { describeChange } from './changes.js';
import { Notice } from './controls.jsx';

/** Which rule and profile answered, and what the head weighs. `forced`: the profile was applied whatever the rules say. */
export function PreviewFacts({ data, forced = false }) {
  const legacy = data.profile === null;
  const routing = forced && !data.protected ? [] : [
    ['Профиль', legacy ? 'legacy: прежний путь' : data.profile],
    ['Правило', legacy ? '—' : data.rule || 'по умолчанию (default_profile)'],
    ['Ревизия', data.rev === null ? 'черновик' : String(data.rev)],
  ];
  const rows = [
    ...routing,
    ['Назначение', data.purpose || '—'],
    ['Клиент', data.client || '—'],
    ['Модель', data.model || '—'],
    ['Размер head', data.head_bytes === null ? '—' : `${fmtInt(data.head_bytes)} байт${data.tokens_estimate === null ? '' : ` · ≈ ${fmtInt(data.tokens_estimate)} токенов`}`],
  ];
  return (
    <>
      <dl className="p-facts">
        {rows.map(([name, value]) => <div key={name}><dt>{name}</dt><dd className="mono">{value}</dd></div>)}
      </dl>
      {data.protected && (
        <Notice tone="warn" icon={TriangleAlert}>
          Это служебный запрос под защитой: он идёт с system клиента как есть, ваши правила к нему не применяются
        </Notice>
      )}
      {legacy && !forced && (
        <Notice tone="warn" icon={TriangleAlert}>
          Ни одно правило не сработало и профиля по умолчанию нет: действует прежний путь, документ промптов этот запрос не меняет
        </Notice>
      )}
    </>
  );
}

function Changes({ title, items, tone = 'muted', note }) {
  if (!items.length) return null;
  return (
    <section className="p-changes" aria-label={title}>
      <h3>{title}<span className="chip-count num">{fmtInt(items.length)}</span></h3>
      {note && <p className="p-hint">{note}</p>}
      <ul>
        {items.map((item, index) => {
          const change = describeChange(item);
          return (
            <li key={index} className={`tone-${tone}`}>
              <span>{change.title}</span>
              {change.details.length > 0 && (
                <span className="p-change-details mono">{change.details.map(([name, value]) => `${name}: ${value}`).join(' · ')}</span>
              )}
              {change.note && <span className="muted small">{change.note}</span>}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/** Warnings, edits that matched nothing, and what happened to the history and the tools. */
export function PreviewLists({ data }) {
  return (
    <>
      {data.warnings.length > 0 && (
        <section className="p-changes" aria-label="Предупреждения">
          <h3>Предупреждения<span className="chip-count num">{fmtInt(data.warnings.length)}</span></h3>
          <ul>
            {data.warnings.map((warning, index) => <li key={index} className="tone-warn"><span>{describeChange(warning).title}</span></li>)}
          </ul>
        </section>
      )}
      <Changes title="Не сработали" items={data.edits_unmatched} tone="warn"
        note="Правки, которые ничего не нашли в этом запросе. Это не ошибка: на другом запросе они могут сработать" />
      <Changes title="Изменения истории" items={data.history_changes} />
      <Changes title="Изменения инструментов" items={data.tools_changes} />
    </>
  );
}
