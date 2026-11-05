import { Fragment, useState } from 'react';
import { Plus, Trash } from 'lucide-react';
import { Button, Tag } from '../ui.jsx';
import { fmtInt } from '../format.js';
import { Area, Disclosure, IconBtn, Line, Notice } from './controls.jsx';
import { isObject, removeText, renameText, setRelayText, textNameError, textUsage, textsOf } from './draft.js';
import { MAX_TEXT_BYTES } from './template.js';

const RELAY_TEXTS = [
  { name: 'role', title: 'Роль relay', where: 'Запросы без system клиента и без инструментов («задачи relay»), а также переменная {{role}}', vars: [] },
  { name: 'native_role', title: 'Роль для клиента с инструментами', where: 'Клиент со своими инструментами, который не прислал system, а также переменная {{native_role}}', vars: [] },
  { name: 'structured', title: 'Обёртка структурированного ответа', where: 'Оформление контракта structured в head. Контракт присутствует всегда', vars: ['{{default}}', '{{schema}}', '{{schema_name}}'] },
  { name: 'reminder', title: 'Обёртка напоминаний', where: 'Оборачивает system-сообщения посреди диалога при режиме «Обернуть как напоминание»', vars: ['{{text}}'] },
  { name: 'codex_base', title: 'Базовые инструкции Codex', where: 'Публикуются в каталоге /codex/models как base_instructions: одна строка для всех моделей или маски моделей', vars: [] },
];

function FromFile({ value, onReplace }) {
  return (
    <div className="p-field">
      <p className="p-filechip mono">из файла: {value.file}</p>
      <p className="p-hint">Текст лежит в файле рядом с prompts.json. Правьте файл на диске: relay перечитает его сам</p>
      {onReplace && <div><Button onClick={onReplace}>Заменить текстом в документе</Button></div>}
    </div>
  );
}

function TextBody({ label, value, onChange, path, rows = 6, placeholder }) {
  if (isObject(value) && typeof value.file === 'string') return <FromFile value={value} onReplace={() => onChange('')} />;
  return <Area label={label} labelHidden value={typeof value === 'string' ? value : ''} onChange={onChange} rows={rows} limit={MAX_TEXT_BYTES} path={path} placeholder={placeholder} />;
}

function NamedText({ doc, setDoc, name }) {
  const value = doc.texts[name];
  const used = textUsage(doc, name);
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(name);
  const taken = Object.keys(doc.texts).filter((key) => key !== name);
  const error = renaming ? textNameError(draft, taken) : null;
  const set = (next) => setDoc((current) => ({ ...current, texts: { ...current.texts, [name]: next } }));
  return (
    <article className="p-text" aria-label={`Текст ${name}`}>
      <div className="p-text-head">
        <div>
          <h3 className="mono">{name}</h3>
          <p className="p-hint">Вставка в шаблон: <code>{`{{text:${name}}}`}</code></p>
        </div>
        <div className="head-actions">
          <Button onClick={() => { setDraft(name); setRenaming(!renaming); }} aria-expanded={renaming}>Переименовать</Button>
          <IconBtn icon={Trash} tone="danger" label={`Удалить текст ${name}`}
            onClick={() => {
              if (used.length && !window.confirm(`Текст ${name} используется в профилях: ${used.join(', ')}. Убрать его всё равно?`)) return;
              setDoc((current) => removeText(current, name));
            }} />
        </div>
      </div>
      {renaming && (
        <form className="p-rename" onSubmit={(event) => { event.preventDefault(); if (!error && draft !== name) setDoc((current) => renameText(current, name, draft)); setRenaming(false); }}>
          <Line label="Новое имя" value={draft} onChange={setDraft} hint="Ссылки в шаблонах и правках профилей перейдут на новое имя" />
          {error && draft !== name && <p className="p-error">{error}</p>}
          <div className="head-actions">
            <Button type="submit" tone="primary" disabled={!!error || draft === name}>Переименовать</Button>
            <Button onClick={() => setRenaming(false)}>Отмена</Button>
          </div>
        </form>
      )}
      <TextBody label={`Текст ${name}`} value={value} onChange={set} path={`texts.${name}`} />
      <p className="p-hint">{used.length ? `Используется в профилях: ${used.join(', ')}` : 'Пока нигде не используется'}</p>
    </article>
  );
}

function NamedTexts({ doc, setDoc }) {
  const texts = textsOf(doc);
  const names = Object.keys(texts);
  const [name, setName] = useState('');
  const error = textNameError(name, names);
  return (
    <section className="panel p-texts" aria-label="Именованные тексты">
      <div className="panel-head">
        <div>
          <h2>Именованные тексты</h2>
          <p className="panel-meta">Короткие блоки, которые вставляются в профили через <code>{'{{text:имя}}'}</code></p>
        </div>
      </div>
      {!names.length && <p className="p-empty-line muted">Текстов пока нет</p>}
      {names.map((key) => <NamedText key={key} doc={doc} setDoc={setDoc} name={key} />)}
      <form className="p-add-text" onSubmit={(event) => { event.preventDefault(); if (!error) { setDoc((current) => ({ ...current, texts: { ...textsOf(current), [name]: '' } })); setName(''); } }}>
        <Line label="Новый текст: имя" value={name} onChange={setName} placeholder="my_rules" />
        <Button type="submit" icon={Plus} disabled={!!error}>Добавить текст</Button>
        <p className="p-hint p-add-hint">Имя без пробелов, фигурных скобок и двоеточий</p>
      </form>
    </section>
  );
}

// codex_base is one string for every model or a map of model masks to strings.
function CodexBase({ value, onChange }) {
  const map = isObject(value) && typeof value.file !== 'string' ? value : null;
  if (!map) {
    return (
      <>
        <TextBody label="Базовые инструкции Codex" value={value} onChange={onChange} path="relay_texts.codex_base" />
        {typeof value === 'string' && <div><Button onClick={() => onChange({ '*': value })}>Разделить по маскам моделей</Button></div>}
      </>
    );
  }
  const entries = Object.entries(map);
  const set = (next) => onChange(Object.fromEntries(next));
  return (
    <div className="p-codex-map">
      {entries.map(([glob, text], index) => (
        <div className="p-codex-row" key={index}>
          <Line label="Маска модели" value={glob} onChange={(next) => set(entries.map(([key, item], i) => [i === index ? next : key, item]))} placeholder="gpt-5*" />
          <Area label={`Инструкции для ${glob || 'маски'}`} value={text} limit={MAX_TEXT_BYTES} rows={5} path={`relay_texts.codex_base.${glob}`}
            onChange={(next) => set(entries.map(([key, item], i) => [key, i === index ? next : item]))} />
          <IconBtn icon={Trash} tone="danger" label={`Убрать маску ${glob || index + 1}`} onClick={() => set(entries.filter((_, i) => i !== index))} />
        </div>
      ))}
      <div className="head-actions">
        <Button icon={Plus} onClick={() => set([...entries, ['', '']])}>Добавить маску</Button>
        <Button onClick={() => onChange(entries[0]?.[1] ?? '')}>Одна строка для всех моделей</Button>
      </div>
      <p className="p-hint">Первая подходящая маска в порядке списка. Модели, которых не поймала ни одна маска, получают встроенный текст</p>
    </div>
  );
}

function RelayText({ doc, setDoc, builtin, info }) {
  const raw = doc.relay_texts?.[info.name];
  const custom = raw !== undefined && raw !== null;
  const standard = builtin.relayTexts[info.name];
  const standardText = typeof standard === 'string' ? standard : standard ? JSON.stringify(standard, null, 2) : '';
  const set = (next) => setDoc((current) => setRelayText(current, info.name, next));
  return (
    <article className="p-text" aria-label={info.title}>
      <div className="p-text-head">
        <div>
          <h3>{info.title}</h3>
          <p className="p-hint">{info.where}</p>
          {info.vars.length > 0 && (
            <p className="p-hint">Переменные: {info.vars.map((variable, index) => <Fragment key={variable}>{index > 0 && ' '}<code>{variable}</code></Fragment>)}</p>
          )}
        </div>
        <div className="head-actions">
          <Tag tone={custom ? 'accent' : 'muted'}>{custom ? 'свой текст' : 'встроенный'}</Tag>
          {custom ? <Button onClick={() => set(null)}>Вернуть встроенный</Button> : <Button onClick={() => set(standardText)}>Изменить</Button>}
        </div>
      </div>
      {custom ? (
        <>
          {info.name === 'codex_base' ? <CodexBase value={raw} onChange={set} />
            : <TextBody label={info.title} value={raw} onChange={set} path={`relay_texts.${info.name}`} />}
          {standardText && (
            <Disclosure title="Встроенный текст" meta={`${fmtInt(standardText.length)} симв.`}>
              <pre className="p-builtin" tabIndex={0}>{standardText}</pre>
            </Disclosure>
          )}
        </>
      ) : standardText ? (
        <div className="p-builtin-wrap">
          <Area label={`Встроенный текст: ${info.title}`} labelHidden value={standardText} onChange={() => {}} rows={info.name === 'codex_base' ? 6 : 4} readOnly />
          <p className="p-hint">Сейчас действует встроенный текст relay. «Изменить» берёт его за основу</p>
        </div>
      ) : (
        <Notice tone="muted">Relay не сообщил встроенный текст: он действует как есть. Если нажать «Изменить», начнёте с пустого поля</Notice>
      )}
    </article>
  );
}

export default function TextsTab({ doc, setDoc, builtin }) {
  return (
    <div className="p-texts-tab">
      <NamedTexts doc={doc} setDoc={setDoc} />
      <section className="panel p-texts" aria-label="Тексты relay">
        <div className="panel-head">
          <div>
            <h2>Встроенные тексты relay</h2>
            <p className="panel-meta">Пока текст не изменён, действует встроенный. Изменённый хранится в prompts.json</p>
          </div>
        </div>
        {RELAY_TEXTS.map((info) => <RelayText key={info.name} doc={doc} setDoc={setDoc} builtin={builtin} info={info} />)}
      </section>
    </div>
  );
}
