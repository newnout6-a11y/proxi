import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Copy, Pencil, Plus, TriangleAlert } from 'lucide-react';
import { Button } from '../ui.jsx';
import { fmtInt } from '../format.js';
import { Area, Disclosure, Line, Notice, Select, Tokens } from './controls.jsx';
import EditList from './EditList.jsx';
import { isObject, patchProfile, profileNameError } from './draft.js';
import { useFocusLater } from './hooks.js';
import { MID_SYSTEM_MODES } from './ops.js';
import { publishedModels } from './publish.js';
import { insertAt, problemText, templateProblems, variableNames } from './template.js';

const EFFORTS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];
const VERBOSITIES = ['low', 'medium', 'high'];

function HeadEditor({ profile, name, builtin, textNames, onChange }) {
  const area = useRef(null);
  const caret = useRef(null);
  const [hint, setHint] = useState('');
  const head = typeof profile.head === 'string' ? profile.head : '';
  const fromFile = isObject(profile.head) ? profile.head : null;
  const variables = useMemo(() => builtin.variables.map((variable) => variable.name), [builtin.variables]);
  const problems = useMemo(() => templateProblems(head, variables, textNames), [head, variables, textNames]);
  const used = new Set(variableNames(head));
  const relayOnly = builtin.relayTaskVariables.filter((variable) => used.has(variable));
  const missingClient = !fromFile && head && !used.has('client_system') && !used.has('client_system_raw');

  useLayoutEffect(() => {
    if (caret.current === null || !area.current) return;
    area.current.focus();
    area.current.setSelectionRange(caret.current, caret.current);
    caret.current = null;
  });

  const insert = (snippet) => {
    const element = area.current;
    const start = element ? element.selectionStart : head.length;
    const end = element ? element.selectionEnd : head.length;
    const result = insertAt(head, start, end, snippet);
    caret.current = result.caret;
    onChange({ head: result.value });
  };

  if (fromFile) {
    return (
      <div className="p-field">
        <span className="p-label">Шаблон head</span>
        <p className="p-filechip mono">из файла: {fromFile.file}</p>
        <p className="p-hint">Шаблон лежит в файле рядом с prompts.json. Правьте файл на диске: relay перечитает его сам. Или замените файл текстом в документе</p>
        <div><Button onClick={() => onChange({ head: '{{client_system}}' })}>Заменить текстом</Button></div>
      </div>
    );
  }
  return (
    <div className="p-head-editor">
      <Area label="Шаблон head" value={head} onChange={(value) => onChange({ head: value })} rows={9} limit={builtin.limits.textBytes} areaRef={area}
        path={`profiles.${name}.head`} placeholder={'Мои правила…\n\n{{client_system}}'}
        hint="Текст, который модель получит как system. Переменные в двойных фигурных скобках; \{{ — буквальные скобки" />
      <div className="p-palette" role="group" aria-label="Вставить переменную в курсор">
        {builtin.variables.map((variable) => (
          <button type="button" key={variable.name} className="chip mono" onClick={() => insert(`{{${variable.name}}}`)}
            onMouseEnter={() => setHint(`{{${variable.name}}} — ${variable.description}`)} onMouseLeave={() => setHint('')}
            onFocus={() => setHint(`{{${variable.name}}} — ${variable.description}`)} onBlur={() => setHint('')}>
            {`{{${variable.name}}}`}
          </button>
        ))}
        {textNames.map((text) => (
          <button type="button" key={`text:${text}`} className="chip mono" onClick={() => insert(`{{text:${text}}}`)}
            onMouseEnter={() => setHint(`{{text:${text}}} — именованный текст`)} onMouseLeave={() => setHint('')}
            onFocus={() => setHint(`{{text:${text}}} — именованный текст`)} onBlur={() => setHint('')}>
            {`{{text:${text}}}`}
          </button>
        ))}
      </div>
      <p className="p-hint p-palette-hint" aria-live="polite">{hint || 'Нажмите на переменную: она встанет в курсор. Наведите, чтобы прочитать, что она значит'}</p>
      {problems.map((problem, index) => <Notice key={index} tone="warn" icon={TriangleAlert}>{problemText(problem)}</Notice>)}
      {relayOnly.length > 0 && (
        <p className="p-hint">{relayOnly.map((variable) => `{{${variable}}}`).join(', ')}: только для запросов, которыми владеет relay. Для запросов клиента пусто</p>
      )}
      {missingClient && <p className="p-hint">В шаблоне нет {'{{client_system}}'}: system клиента в этот head не попадёт</p>}
    </div>
  );
}

function Generation({ profile, name, onChange }) {
  const generation = isObject(profile.generation) ? profile.generation : {};
  const set = (key) => (value) => onChange({ generation: { ...generation, [key]: value || null } });
  const options = (values, current) => [{ value: '', label: 'не трогать' }, ...[...new Set([...values, ...(current && !values.includes(current) ? [current] : [])])].map((value) => ({ value, label: value }))];
  return (
    <div className="p-pair">
      <Select label="Effort" value={generation.effort || ''} onChange={set('effort')} path={`profiles.${name}.generation.effort`}
        options={options(EFFORTS, generation.effort)} hint="Подменяет effort клиента по тем же правилам допустимости, что и в настройках" />
      <Select label="Verbosity" value={generation.verbosity || ''} onChange={set('verbosity')} path={`profiles.${name}.generation.verbosity`}
        options={options(VERBOSITIES, generation.verbosity)} hint="Подменяет verbosity клиента, если модель его принимает" />
    </div>
  );
}

function Publish({ profile, name, onChange, catalog }) {
  // Emptying the list of masks keeps the list open: the user is retyping it, not switching publishing off
  const [listing, setListing] = useState(false);
  const mode = Array.isArray(profile.publish_as_model) || (listing && !profile.publish_as_model) ? 'list' : profile.publish_as_model === true ? 'all' : 'off';
  const globs = Array.isArray(profile.publish_as_model) ? profile.publish_as_model : [];
  const choose = (id) => {
    setListing(id === 'list');
    onChange({ publish_as_model: id === 'off' ? false : id === 'all' ? true : globs.length ? globs : ['claude-*'] });
  };
  const published = mode === 'off' ? [] : publishedModels(mode === 'all' ? true : globs, catalog);
  const example = (published && published[0]) || 'claude-opus-5-5';
  const shown = published ? published.slice(0, 6) : [];
  return (
    <div className="p-publish">
      <fieldset className="p-radio-group">
        <legend>Каталог моделей</legend>
        {[['off', 'Не публиковать'], ['all', 'Опубликовать для всех моделей каталога'], ['list', 'Только для моделей по маске']].map(([id, label]) => (
          <label className="check" key={id}>
            <input type="radio" name={`publish-${name}`} checked={mode === id} onChange={() => choose(id)} />
            <span>{label}</span>
          </label>
        ))}
      </fieldset>
      {mode === 'list' && (
        <Tokens label="Маски моделей" values={globs} onChange={(values) => onChange({ publish_as_model: values.length ? values : false })}
          placeholder="claude-opus-*" path={`profiles.${name}.publish_as_model`}
          hint="Маска сравнивается с полным id модели из каталога, без учёта регистра: * — любые символы, ? — один символ" />
      )}
      {mode === 'list' && !globs.length && <p className="p-hint">Без масок профиль не публикуется</p>}
      {mode === 'list' && globs.length > 0 && published && published.length === 0 && (
        <Notice tone="warn" icon={TriangleAlert} className="p-publish-none">
          <p>
            Маски не подходят ни к одной модели каталога с маршрутом, поэтому профиль не появится в каталоге. Маска сравнивается с полным id:
            например, у GLM он <code>zai-org/GLM-5.3</code>, поэтому подойдёт <code>*glm*</code> или <code>zai-org/GLM-*</code>
          </p>
        </Notice>
      )}
      {mode !== 'off' && published && published.length > 0 && (
        <p className="p-hint p-publish-list">
          В каталоге появятся ({fmtInt(published.length)}): {shown.map((id, index) => (
            <span key={id}>{index ? ', ' : ''}<code>{`${id}@${name}`}</code></span>
          ))}{published.length > shown.length ? ` и ещё ${fmtInt(published.length - shown.length)}` : ''}
        </p>
      )}
      {mode === 'all' || globs.length > 0 ? <p className="p-hint">Клиент выбирает профиль, просто называя такую модель: <code>{`${example}@${name}`}</code></p> : null}
    </div>
  );
}

/** The form of one profile: template, edits of the client's system, history, tools, generation and publishing. */
export default function ProfileEditor({ doc, setDoc, name, builtin, textNames, usage, onRename, onDuplicate, onDelete, onAddRule, catalog = null }) {
  const profile = doc.profiles[name];
  const [renaming, setRenaming] = useState(false);
  const [nameDraft, setNameDraft] = useState(name);
  const root = useRef(null);
  const focusLater = useFocusLater();
  const taken = Object.keys(doc.profiles).filter((key) => key !== name);
  const nameError = renaming ? profileNameError(nameDraft, taken, builtin.profileName ?? undefined) : null;
  // the form opens on its field and closes onto the button that opened it
  const rename = (open) => {
    setNameDraft(name);
    setRenaming(open);
    focusLater(() => root.current?.querySelector(open ? '.p-rename input' : '[data-act="rename"]'));
  };
  const patch = (change) => setDoc((current) => patchProfile(current, name, change));
  const systemEdits = profile.client_system?.edits || [];
  const userBlocks = profile.history?.user_blocks || [];
  const toolEdits = profile.tools?.edits || [];
  const mode = profile.history?.mid_system || 'keep';
  const hasGeneration = isObject(profile.generation) && Object.values(profile.generation).some((value) => value !== null && value !== undefined && value !== '');
  const modes = MID_SYSTEM_MODES.filter((item) => builtin.editOps.mid_system.includes(item.id) || item.id === mode);
  const used = usage.rules.length + (usage.isDefault ? 1 : 0);

  return (
    <div className="p-profile" ref={root}>
      <div className="p-profile-head">
        <div>
          <h2 className="mono">{name}</h2>
          <p className="p-hint">Имя профиля — суффикс модели: <code>{`<модель>@${name}`}</code></p>
        </div>
        <div className="head-actions">
          <Button icon={Pencil} data-act="rename" onClick={() => rename(!renaming)} aria-expanded={renaming}>Переименовать</Button>
          <Button icon={Copy} onClick={onDuplicate}>Дублировать</Button>
          <Button tone="danger" disabled={used > 0} title={used > 0 ? 'Профиль используется правилами: сначала измените их' : undefined} onClick={onDelete}>Удалить</Button>
        </div>
      </div>
      {renaming && (
        <form className="p-rename" onSubmit={(event) => {
          event.preventDefault();
          if (!nameError && nameDraft !== name) { onRename(nameDraft); return; }
          rename(false);
        }}>
          <Line label="Новое имя" value={nameDraft} onChange={setNameDraft} hint="Правила, которые используют профиль, перейдут на новое имя" />
          {nameError && nameDraft !== name && <p className="p-error">{nameError}</p>}
          <div className="head-actions">
            <Button type="submit" tone="primary" disabled={!!nameError || nameDraft === name}>Переименовать</Button>
            <Button onClick={() => rename(false)}>Отмена</Button>
          </div>
        </form>
      )}
      {used > 0 ? (
        <p className="p-usage">
          Применяется: {usage.rules.map(({ rule, index }) => <span key={index} className="tag">{`${index + 1}. ${rule.name || 'правило'}`}</span>)}
          {usage.isDefault && <span className="tag">профиль по умолчанию</span>}
        </p>
      ) : (
        <Notice tone="warn" icon={TriangleAlert}>
          <span>Ни одно правило не использует этот профиль, поэтому он ничего не меняет. </span>
          <button type="button" className="link-button" onClick={onAddRule}><Plus size={14} strokeWidth={1.5} aria-hidden="true" /> Добавить правило</button>
        </Notice>
      )}
      <Line label="Описание" value={profile.description || ''} mono={false} onChange={(description) => patch({ description })} placeholder="Для чего этот профиль"
        path={`profiles.${name}.description`} />
      <HeadEditor profile={profile} name={name} builtin={builtin} textNames={textNames} onChange={patch} />

      <Disclosure title="Правки system клиента" meta={systemEdits.length ? fmtInt(systemEdits.length) : 'нет'} defaultOpen={systemEdits.length > 0}>
        <p className="p-hint">Применяются по порядку к system клиента до подстановки в {'{{client_system}}'}. Правка, которая ничего не нашла, не ошибка: предпросмотр покажет, что она не сработала</p>
        <EditList scope="client_system" edits={systemEdits} ops={builtin.editOps.client_system} path={`profiles.${name}.client_system.edits`}
          onChange={(edits) => patch({ client_system: { ...profile.client_system, edits } })} />
      </Disclosure>
      <Disclosure title="История диалога" meta={mode !== 'keep' || userBlocks.length ? 'изменена' : 'как есть'} defaultOpen={mode !== 'keep' || userBlocks.length > 0}>
        <Select label="Системные сообщения посреди диалога" value={mode} path={`profiles.${name}.history.mid_system`}
          options={modes.map((item) => ({ value: item.id, label: item.label }))}
          hint={`${modes.find((item) => item.id === mode)?.hint || ''}. Ведущие system и developer (head) сюда не относятся`}
          onChange={(value) => patch({ history: { ...profile.history, mid_system: value } })} />
        <h3 className="p-subhead">Правки сообщений пользователя</h3>
        <p className="p-hint">Текст сообщений user и результатов инструментов. Изображения и вызовы инструментов не трогаются. Смена истории посреди диалога сбрасывает кэш: диалоги закреплены за своей ревизией</p>
        <EditList scope="user_blocks" edits={userBlocks} ops={builtin.editOps.user_blocks} path={`profiles.${name}.history.user_blocks`}
          onChange={(edits) => patch({ history: { ...profile.history, user_blocks: edits } })} />
      </Disclosure>
      <Disclosure title="Инструменты" meta={toolEdits.length ? fmtInt(toolEdits.length) : 'нет'} defaultOpen={toolEdits.length > 0}>
        <p className="p-hint">Описания инструментов клиента. Скрытие инструмента из начатого диалога рискованно: у диалога с подписанным мышлением оно сбросит кэш</p>
        <EditList scope="tools" edits={toolEdits} ops={builtin.editOps.tools} path={`profiles.${name}.tools.edits`}
          onChange={(edits) => patch({ tools: { ...profile.tools, edits } })} />
      </Disclosure>
      <Disclosure title="Генерация" meta={hasGeneration ? 'задана' : 'не трогать'} defaultOpen={hasGeneration}>
        <Generation profile={profile} name={name} onChange={patch} />
      </Disclosure>
      <Disclosure title="Публикация как модель" meta={profile.publish_as_model ? 'да' : 'нет'} defaultOpen={!!profile.publish_as_model}>
        <Publish profile={profile} name={name} onChange={patch} catalog={catalog} />
      </Disclosure>
    </div>
  );
}
