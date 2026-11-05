import { Fragment, useEffect, useMemo, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { RotateCcw, Save, Search, SlidersHorizontal } from 'lucide-react';
import { api } from '../api.js';
import { usePolling } from '../useJournal.js';
import { Dot, Page, ease } from '../motion.jsx';
import { Button, CopyButton, Tag, Toggle, useToast } from '../ui.jsx';
import { fmtInt, fmtTokens, isNum } from '../format.js';

// Each group answers one question an operator actually has.
export const GROUPS = [
  { id: 'routing', title: 'Аккаунты и маршрутизация', hint: 'Когда аккаунт уходит в резерв и как часто проверяются баланс и сессия' },
  { id: 'model', title: 'Модель и мышление', hint: 'Что получает клиент, если не указал модель или effort сам' },
  { id: 'instructions', title: 'Системные инструкции', hint: 'Задайте модели роль, стиль работы и границы' },
  { id: 'retries', title: 'Повторы и диагностика', hint: 'Сколько раз повторять пустой ответ и сколько диагностики хранить' },
  { id: 'cache', title: 'Кэш промпта', hint: 'Прогрев держит кэш тёплым, чтобы следующий ход читал его, а не платил за запись' },
  { id: 'exec', title: 'Локальные команды', hint: 'Может ли модель запускать команды на этой машине' },
  { id: 'limits', title: 'Лимиты и таймауты', hint: 'Защита relay от перегрузки и зависших запросов' },
  { id: 'api', title: 'API для клиентов', hint: 'Какие протоколы relay принимает от IDE' },
];

const groupOf = (field) => field.group || 'limits';
const CATALOG_FOLD = 10;
const OPTION_LABELS = {
  session_mode: { per_conversation: 'сессия на каждый диалог', per_request: 'сессия на каждый запрос', shared: 'одна общая сессия' },
  exec_policy: { off: 'выключены', allowlist: 'только из списка разрешённых', allow_all: 'любые команды' },
  reasoning_effort: { auto: 'по умолчанию модели' },
};

function Field({ name, field, value, onChange, pending, dirty }) {
  const id = `setting-${name}`;
  const hint = `${id}-hint`;
  let control;
  if (field.type === 'boolean') {
    control = <Toggle id={id} checked={!!value} label={field.label} onChange={onChange} />;
  } else if (field.enum) {
    control = (
      <select id={id} value={value ?? ''} aria-describedby={hint} onChange={(e) => onChange(e.target.value)}>
        {field.enum.map((option) => <option key={option} value={option}>{OPTION_LABELS[name]?.[option] || option}</option>)}
      </select>
    );
  } else if (field.type === 'string') {
    control = <input id={id} type="text" value={value ?? ''} aria-describedby={hint} onChange={(e) => onChange(e.target.value)} />;
  } else {
    control = (
      <span className="number-field">
        <input id={id} type="number" min={field.min} max={field.max} step={field.type === 'integer' ? 1 : 'any'} value={value ?? ''} aria-describedby={hint}
          onChange={(e) => {
            const raw = e.target.value;
            onChange(raw === '' ? '' : field.type === 'integer' ? parseInt(raw, 10) : parseFloat(raw));
          }} />
        {field.unit && <span className="unit">{field.unit}</span>}
      </span>
    );
  }
  const description = field.description || null;
  return (
    <div className={`setting ${pending ? 'pending' : ''} ${dirty ? 'dirty' : ''}`}>
      <label htmlFor={id}>
        <strong>{field.label}</strong>
        {description && <small id={hint}>{description}</small>}
      </label>
      <div className="setting-control">
        {pending && <Tag tone="warn" title="Сохранено, применится после перезапуска">ждёт перезапуска</Tag>}
        {!field.restart_required && !pending && <span className="setting-when">сразу</span>}
        {control}
      </div>
    </div>
  );
}

const PROMPT_MODES = [
  { id: 'standalone', title: 'Самостоятельные запросы', text: 'Своя роль для запросов без системных инструкций клиента' },
  { id: 'append', title: 'Дополнить клиента', text: 'Добавить свою роль после системных инструкций IDE' },
  { id: 'replace', title: 'Заменить роль клиента', text: 'Использовать свою роль вместо системных инструкций IDE' },
];

const ROLE_KEY = 'relay_system_prompt';
const textBytes = (text) => new TextEncoder().encode(text).length;
// The server judges the same pair (role bytes against max_prompt_bytes) and has the last word.
const roleLimit = (field, promptCap) => {
  const cap = field?.max_bytes ?? Infinity;
  return promptCap > 0 ? Math.min(cap, Math.floor(promptCap * (field?.max_prompt_share ?? 1))) : cap;
};

function SystemInstructions({ settings, saved, roles, field, bytes, limit, onChange, pending, disabled }) {
  const relay = roles.find((item) => item.id === 'relay');
  const others = roles.filter((item) => item.id !== 'relay');
  const standard = settings[ROLE_KEY] === null;
  const text = standard ? relay?.text || '' : settings[ROLE_KEY] ?? '';
  const blank = !standard && !text.trim();
  const over = !standard && bytes > limit;
  const capped = isNum(field?.max_bytes) && limit < field.max_bytes;
  const scope = relay ? `${relay.title}. ${relay.applies_to}. ` : '';
  // Clearing the box starts from a text to edit, never from an empty (disabled) role.
  const customise = (checked) => onChange(ROLE_KEY, checked ? null : (typeof saved === 'string' && saved) || relay?.text || '');
  return (
    <div className="prompt-editor">
      <fieldset className="prompt-modes" disabled={disabled}>
        <legend>Как применять роль</legend>
        {PROMPT_MODES.map((mode) => (
          <label key={mode.id} className={`prompt-mode ${settings.relay_system_prompt_mode === mode.id ? 'selected' : ''}`}>
            <input type="radio" name="system-mode" value={mode.id} checked={settings.relay_system_prompt_mode === mode.id}
              onChange={() => onChange('relay_system_prompt_mode', mode.id)} />
            <span><strong>{mode.title}</strong><small>{mode.text}</small></span>
          </label>
        ))}
      </fieldset>
      <div className="prompt-toolbar">
        <label className="check"><input type="checkbox" checked={standard} disabled={disabled}
          onChange={(e) => customise(e.target.checked)} />Стандартная роль relay</label>
        {pending && <Tag tone="warn">ждёт перезапуска</Tag>}
      </div>
      <label className="prompt-label" htmlFor="system-instructions">Инструкции для модели</label>
      <textarea id="system-instructions" rows={standard ? 3 : 7} value={text} disabled={disabled || standard}
        aria-describedby="system-instructions-hint" aria-invalid={over || undefined} spellCheck={false}
        placeholder={standard ? 'Используется встроенная роль relay' : 'Ты — инженер моего проекта. Отвечай по-русски. Проверяй изменения тестами. Перед удалением данных запрашивай подтверждение.'}
        onChange={(e) => onChange(ROLE_KEY, e.target.value)} />
      {blank && (
        <div className="banner tone-warn" role="status">
          Роль отключена: модель получит запросы без роли relay.
          {settings.relay_system_prompt_mode === 'replace' ? ' В режиме «Заменить роль клиента» инструкции IDE тоже не будут отправлены.' : ''}
          {' '}Вернуть встроенную роль можно флажком «Стандартная роль relay»
        </div>
      )}
      <div className="prompt-footer" id="system-instructions-hint">
        {standard ? (
          <p>{scope}Снимите флажок, чтобы задать собственные инструкции: они заменят обе стандартные роли</p>
        ) : over ? (
          <p className="neg-text" role="alert">
            {capped
              ? `Роль не помещается: при лимите «Размер промпта» ей отведено не больше ${fmtInt(limit)} байт. Сократите роль или увеличьте лимит`
              : `Роль слишком длинная: допустимо не больше ${fmtInt(limit)} байт`}
          </p>
        ) : (
          <p>Этот текст заменяет обе стандартные роли relay. Служебные проверки и заголовки IDE сохраняют свои инструкции</p>
        )}
        {!standard && (
          <span className={`mono ${over ? 'neg-text' : ''}`}>
            {fmtInt(text.length)} симв. · {fmtInt(bytes)}{Number.isFinite(limit) ? ` из ${fmtInt(limit)}` : ''} байт
          </span>
        )}
      </div>
      {standard && others.map((item) => (
        <Fragment key={item.id}>
          <label className="prompt-label" htmlFor={`system-default-${item.id}`}>{item.title}</label>
          <textarea id={`system-default-${item.id}`} rows={3} value={item.text} readOnly spellCheck={false}
            aria-describedby={`system-default-${item.id}-hint`} />
          <div className="prompt-footer" id={`system-default-${item.id}-hint`}><p>{item.applies_to}</p></div>
        </Fragment>
      ))}
    </div>
  );
}

function Connection({ overview }) {
  const rows = [['OpenAI-совместимый адрес', overview?.connection?.openai_base_url], ['Anthropic-совместимый адрес', overview?.connection?.anthropic_base_url]];
  return (
    <section id="settings-connection" className="panel connect" aria-label="Подключение IDE">
      <div className="panel-head">
        <h2>Подключение IDE</h2>
        <span className="status"><Dot tone={!overview ? 'muted' : overview.session?.error ? 'neg' : 'pos'} />
          {!overview ? 'проверяем сессию' : overview.session?.error ? 'сессия Retool с ошибкой' : 'сессия Retool активна'}</span>
      </div>
      {rows.map(([label, value]) => (
        <div className="connect-row" key={label}>
          <span>{label}</span>
          <code>{value || '—'}</code>
          {value && <CopyButton value={value} label={`Копировать: ${label}`} />}
        </div>
      ))}
      <p className="muted small">Ключ — relay key: заголовок <code>Authorization: Bearer</code> или <code>x-api-key</code></p>
    </section>
  );
}

// Why a model has no route, from the relay's own reason (route_blocker); older relays only said "no resource".
function routeLabel(model) {
  if (model.routable) return 'маршрут есть';
  if (model.route_blocker === 'no_accounts') return 'нет включённых аккаунтов';
  if (model.route_blocker === 'blocked') return model.route_status === 'unsupported' ? 'маршрут не поддерживается' : 'нужен BYOK или retention';
  return 'нет ресурса';
}

function Catalog({ apiKey, onUnauthorized, onData }) {
  const [data, setData] = useState(null);
  const [query, setQuery] = useState('');
  const [onlyRoutable, setOnlyRoutable] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [error, setError] = useState('');
  usePolling(async () => {
    try { const result = await api(apiKey, 'models'); setData(result); onData(result); setError(''); } catch (failure) { if (failure.status === 401) onUnauthorized(); else setError(failure.message); }
  }, 60000, [apiKey]);
  const all = data?.data || [];
  const items = all.filter((model) => (!onlyRoutable || model.routable)
    && (!query || `${model.id} ${model.label} ${model.provider}`.toLowerCase().includes(query.toLowerCase())));
  // The default model leads; a long catalog is folded unless searched or expanded.
  items.sort((a, b) => (b.id === data?.default_model) - (a.id === data?.default_model));
  const shown = expanded || query ? items : items.slice(0, CATALOG_FOLD);
  return (
    <section id="settings-catalog" className="panel model-table" aria-label="Каталог моделей">
      <div className="panel-head model-head">
        <div>
          <h2>Каталог моделей</h2>
          <p className="panel-meta">всего {fmtInt(all.length)} · с маршрутом {fmtInt(all.filter((m) => m.routable).length)}</p>
        </div>
        <div className="head-actions">
          <div className="search compact">
            <Search size={16} strokeWidth={1.5} aria-hidden="true" />
            <input aria-label="Поиск модели" placeholder="Поиск" value={query} onChange={(e) => setQuery(e.target.value)} />
          </div>
          <label className="check">
            <input type="checkbox" checked={onlyRoutable} onChange={(e) => setOnlyRoutable(e.target.checked)} />
            <span>Только с маршрутом</span>
          </label>
        </div>
      </div>
      {error && <p className="banner tone-neg" role="alert">Не удалось загрузить каталог: {error}</p>}
      {!data && !error && <p className="model-empty muted" role="status">Загружаем модели</p>}
      <div className="model-cols" aria-hidden="true"><span>Модель</span><span>Провайдер</span><span>Контекст</span><span>Управление ответом</span><span>Доступность</span></div>
      {shown.map((model) => (
        <article key={model.id} className={`model-card ${model.id === data?.default_model ? 'is-default' : ''}`}>
          <span className="model-name" title={`${model.label} · ${model.id}`}><strong>{model.label}</strong><code>{model.id}</code></span>
          <span className="model-provider">{model.provider}</span>
          <span className="num">{fmtTokens(model.context)}</span>
          <span className="model-effort"><span>Effort: {model.reasoning ? (model.reasoning_effort_levels || []).join(' · ') || 'выбирает провайдер' : 'без мышления'}</span>
            <span>Verbosity: {model.verbosity_levels?.length ? model.verbosity_levels.join(' · ') : 'не поддерживается'}</span>
          </span>
          <span className="model-route" title={model.routable ? undefined : model.route_reason}>
            <Dot tone={model.routable ? 'pos' : 'muted'} />{routeLabel(model)}
            {model.id === data?.default_model && <Tag tone="accent">по умолчанию</Tag>}
          </span>
        </article>
      ))}
      {data && !items.length && <p className="muted small model-empty">Модели не найдены</p>}
      {shown.length < items.length && (
        <div className="model-more">
          <button type="button" className="link-button" onClick={() => setExpanded(true)}>
            показать все {fmtInt(items.length)}
          </button>
        </div>
      )}
    </section>
  );
}

export default function SettingsView({ apiKey, overview, onUnauthorized, unsaved }) {
  const toast = useToast();
  const [state, setState] = useState(null);
  const [draft, setDraft] = useState({});
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [models, setModels] = useState(null);

  usePolling(async () => {
    try {
      setState(await api(apiKey, 'settings'));
      setLoadError('');
    } catch (failure) {
      if (failure.status === 401) onUnauthorized();
      else setLoadError(failure.message);
    }
  }, 30000, [apiKey]);

  const changes = useMemo(() => Object.fromEntries(Object.entries(draft)
    .filter(([key, value]) => state && value !== state.settings[key]
      && (value !== '' || state.schema[key]?.type === 'string'))), [draft, state]);
  const dirty = Object.keys(changes).length;
  // The draft lives here, so the shell asks before a section change would drop it.
  useEffect(() => {
    if (!unsaved) return undefined;
    unsaved.current = dirty;
    return () => { unsaved.current = 0; };
  }, [dirty, unsaved]);

  async function save() {
    const submitted = { ...changes };
    setSaving(true);
    try {
      const result = await api(apiKey, 'settings', { method: 'PATCH', body: { settings: submitted } });
      setState(result);
      setDraft((current) => Object.fromEntries(Object.entries(current)
        .filter(([key, value]) => !(key in submitted) || value !== submitted[key])));
      toast({
        tone: 'success', title: 'Настройки сохранены',
        text: result.applied.length ? `Применено сразу: ${result.applied.map((key) => state.schema[key]?.label || key).join(', ')}` : 'Изменения вступят в силу после перезапуска relay',
      });
    } catch (failure) {
      if (failure.status === 401) onUnauthorized();
      else toast({ tone: 'error', title: 'Не сохранено', text: failure.message });
    } finally {
      setSaving(false);
    }
  }

  const schema = state?.schema || {};
  const merged = { ...state?.settings, ...draft };
  const selectedModel = models?.data.find((model) => model.id === merged.model);
  const effortValue = merged.reasoning_effort;
  // With approximation on, the relay takes the nearest level itself, so no level is an error.
  const approximate = !!merged.allow_effort_downgrade;
  const effortOptions = selectedModel ? ['auto', ...(selectedModel.reasoning_effort_levels || [])] : null;
  const unsupportedEffort = effortOptions && effortValue && !effortOptions.includes(effortValue);
  const warnEffort = unsupportedEffort && !approximate;
  const effortChoices = approximate && schema.reasoning_effort?.enum ? schema.reasoning_effort.enum
    : effortOptions && [...new Set([...effortOptions, ...(unsupportedEffort ? [effortValue] : [])])];
  const role = merged[ROLE_KEY];
  const roleBytes = useMemo(() => (typeof role === 'string' ? textBytes(role) : 0), [role]);
  const limit = roleLimit(schema[ROLE_KEY], Number(merged.max_prompt_bytes));
  // Like the server, judge the role only when this save touches the role or the cap.
  const roleBlocked = roleBytes > limit && (ROLE_KEY in changes || 'max_prompt_bytes' in changes);
  const roles = state?.default_system_prompts
    || (state?.default_system_prompt ? [{ id: 'relay', title: 'Роль relay по умолчанию', text: state.default_system_prompt }] : []);
  const byGroup = {};
  for (const [key, field] of Object.entries(schema)) (byGroup[groupOf(field)] ||= []).push(key);

  return (
    <Page className="settings">
      <header className="page-head">
        <div>
          <h1>Настройки</h1>
          <p className="page-meta">Модель, системная роль и поведение вашего relay</p>
        </div>
        <div className="head-actions">
          {state?.restart_required && <Tag tone="warn">ждут перезапуска: {state.pending_restart.length}</Tag>}
        </div>
      </header>
      <div className="settings-index" role="navigation" aria-label="Группы настроек">
        <SlidersHorizontal size={16} strokeWidth={1.5} aria-hidden="true" />
        {[...GROUPS.filter((group) => Object.values(state?.schema || {}).some((field) => field.group === group.id)),
          { id: 'connection', title: 'Подключение IDE' }, { id: 'catalog', title: 'Каталог' }].map((group) => (
          <button type="button" key={group.id} onClick={() => document.getElementById(`settings-${group.id}`)?.scrollIntoView({ block: 'start' })}>{group.title}</button>
        ))}
      </div>
      <p className="settings-note">Изменения сохраняются в config.json и применяются после перезапуска. Помеченные «сразу» действуют без перезапуска</p>
      {loadError && <div className="banner tone-neg" role="alert">Не удалось загрузить настройки: {loadError}</div>}
      {!state && !loadError && <div className="panel empty" role="status">Загружаем настройки</div>}
      {state && !state.writable && <div className="banner tone-warn">Файл конфигурации недоступен для записи</div>}
      {warnEffort && <div className="banner tone-warn" role="status">Уровень {effortValue} не поддерживается моделью {selectedModel.label}. Выберите доступный уровень или «по умолчанию модели», либо включите «{schema.allow_effort_downgrade?.label || 'Подбирать ближайший effort'}»</div>}
      {state && GROUPS.map((group) => {
        const keys = byGroup[group.id] || [];
        if (!keys.length) return null;
        return (
          <section id={`settings-${group.id}`} key={group.id} className="panel settings-group" aria-label={group.title}>
            <h2>{group.title}</h2>
            <p className="settings-hint">{group.hint}</p>
            {group.id === 'instructions' ? <SystemInstructions settings={merged} saved={state.settings[ROLE_KEY]} roles={roles}
              field={schema[ROLE_KEY]} bytes={roleBytes} limit={limit}
              onChange={(key, value) => setDraft((d) => ({ ...d, [key]: value }))}
              pending={keys.some((key) => state.pending_restart.includes(key))} disabled={!state.writable} /> : keys.map((key) => (
              <Field key={key} name={key} field={key === 'reasoning_effort' && effortChoices
                ? { ...schema[key], enum: effortChoices } : schema[key]}
                value={key in draft ? draft[key] : state.settings[key]}
                pending={state.pending_restart.includes(key)}
                dirty={key in changes}
                onChange={(value) => setDraft((d) => ({ ...d, [key]: value }))} />
            ))}
          </section>
        );
      })}
      <Connection overview={overview} />
      <Catalog apiKey={apiKey} onUnauthorized={onUnauthorized} onData={setModels} />
      <AnimatePresence>
        {dirty > 0 && (
          <motion.div className="savebar" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 8 }}
            transition={{ duration: 0.2, ease }}>
            <span><b className="num">{dirty}</b> несохранённых: <span className="savebar-keys">{Object.keys(changes).map((k) => schema[k]?.label || k).join(', ')}</span>
              {roleBlocked && <span className="neg-text"> · роль не помещается в лимит</span>}</span>
            <Button icon={RotateCcw} onClick={() => setDraft({})}>Отменить</Button>
            <Button icon={Save} tone="primary" onClick={save} disabled={saving || !state?.writable || roleBlocked}>{saving ? 'Сохранение' : 'Сохранить'}</Button>
          </motion.div>
        )}
      </AnimatePresence>
    </Page>
  );
}
