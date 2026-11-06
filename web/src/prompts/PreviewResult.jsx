import { useMemo } from 'react';
import { TriangleAlert } from 'lucide-react';
import { fmtInt, plural } from '../format.js';
import { Tag } from '../ui.jsx';
import { describeChange, describeGroup, describeReport, describeUnmatched, groupChanges } from './changes.js';
import { Notice } from './controls.jsx';
import { isObject } from './draft.js';

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
      {data.head_bytes === 0 && (
        <Notice tone="warn" icon={TriangleAlert}>
          Модель не получит системного текста: head собрался пустым.
          {data.profile !== null && !data.protected && ' Так бывает, когда клиент не прислал system, а шаблон профиля ничего не добавляет от себя'}
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

function Section({ title, count, note, children }) {
  return (
    <section className="p-changes" aria-label={title}>
      <h3>{title}<span className="chip-count num">{fmtInt(count)}</span></h3>
      {note && <p className="p-hint">{note}</p>}
      <ul>{children}</ul>
    </section>
  );
}

/** One line of a list of changes: what, where, how often, and the details under it. */
function Row({ title, where, count = 1, status, details = [], note, tone = 'muted' }) {
  // the spaces between the parts are real text, so a reader that goes by the text does not run the words together
  return (
    <li className={`tone-${tone}`}>
      <span className="p-change-head">
        <span className="p-change-title">{title}</span>
        {where && <>{' '}<span className="p-change-where">{where}</span></>}
        {status && <>{' '}{status}</>}
        {count > 1 && <>{' '}<span className="p-change-count mono">{fmtInt(count)} {plural(count, ['раз', 'раза', 'раз'])}</span></>}
      </span>
      {details.length > 0 && <span className="p-change-details mono">{details.map(([name, value]) => (name ? `${name}: ${value}` : value)).join(' · ')}</span>}
      {note && <span className={tone === 'warn' ? 'p-change-note' : 'muted small'}>{note}</span>}
    </li>
  );
}

const total = (groups) => groups.reduce((sum, group) => sum + group.count, 0);

/**
 * Warnings, what the edits of the client's system did, the edits that matched nothing, and what happened to the history
 * and the tools. A big body leaves one entry per message: they are folded by edit, and an edit the relay skipped is
 * listed apart with its reason. `specs` are the edits of the client's system as the profile that answered has them: the
 * report says which edit did what, and the edit itself says what it looked for.
 */
export function PreviewLists({ data, specs = [] }) {
  const report = data.edits || [];
  const history = useMemo(() => groupChanges(data.history_changes), [data.history_changes]);
  const tools = useMemo(() => groupChanges(data.tools_changes), [data.tools_changes]);
  // The report names every edit of the client's system and says whether it matched, so its misses are not listed twice
  const unmatched = report.length ? data.edits_unmatched.filter((item) => !(isObject(item) && item.scope === 'client_system')) : data.edits_unmatched;
  const applied = (groups) => groups.filter((group) => !group.skipped);
  const skipped = [...history, ...tools].filter((group) => group.skipped);
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
      {report.length > 0 && (
        <Section title="Как сработали правки system клиента" count={report.length}>
          {report.map((row) => {
            const change = describeReport(row, data.edits_unmatched, specs);
            return (
              <Row key={row.index} title={change.title} where={`правка ${change.number}`} details={change.details} tone={row.matched === false ? 'warn' : 'muted'}
                status={row.matched === null ? null : <Tag tone={row.matched ? 'pos' : 'warn'}>{row.matched ? 'сработала' : 'не нашла'}</Tag>} />
            );
          })}
        </Section>
      )}
      {unmatched.length > 0 && (
        <Section title="Не сработали" count={unmatched.length} note="Правки, которые ничего не нашли в этом запросе. Это не ошибка: на другом запросе они могут сработать">
          {unmatched.map((item, index) => <Row key={index} tone="warn" {...describeUnmatched(item)} />)}
        </Section>
      )}
      {[['Изменения истории', applied(history)], ['Изменения инструментов', applied(tools)]].map(([title, groups]) => groups.length > 0 && (
        <Section key={title} title={title} count={total(groups)}>
          {groups.map((group) => <Row key={group.key} {...describeGroup(group)} />)}
        </Section>
      ))}
      {skipped.length > 0 && (
        <Section title="Не применены" count={total(skipped)} note="Правки, которые relay пропустил в этом запросе. Причина указана у каждой">
          {skipped.map((group) => {
            const change = describeGroup(group);
            return <Row key={group.key} tone="warn" {...change} note={change.reason ? `не применена: ${change.reason}` : 'не применена'} />;
          })}
        </Section>
      )}
    </>
  );
}
