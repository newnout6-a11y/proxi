import { useMemo, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { RotateCcw, Save, Search } from 'lucide-react';
import { api } from '../api.js';
import { usePolling } from '../useJournal.js';
import { Dot, Page, ease } from '../motion.jsx';
import { Button, CopyButton, Tag, Toggle, useToast } from '../ui.jsx';
import { fmtInt, fmtTokens } from '../format.js';

// Each group answers one question an operator actually has.
export const GROUPS = [
  { id: 'routing', title: 'Аккаунты и маршрутизация', hint: 'Когда аккаунт уходит в резерв и как часто проверяются баланс и сессия' },
  { id: 'model', title: 'Модель и мышление', hint: 'Что получает клиент, если не указал модель или effort сам' },
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
  let control;
  if (field.type === 'boolean') {
    control = <Toggle id={id} checked={!!value} label={field.label} onChange={onChange} />;
  } else if (field.enum) {
    control = (
      <select id={id} value={value ?? ''} onChange={(e) => onChange(e.target.value)}>
        {field.enum.map((option) => <option key={option} value={option}>{OPTION_LABELS[name]?.[option] || option}</option>)}
      </select>
    );
  } else {
    control = (
      <span className="number-field">
        <input id={id} type="number" min={field.min} max={field.max} step={field.type === 'integer' ? 1 : 'any'} value={value ?? ''}
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
        {description && <small>{description}</small>}
      </label>
      <div className="setting-control">
        {pending && <Tag tone="warn" title="Сохранено, применится после перезапуска">ждёт перезапуска</Tag>}
        {!field.restart_required && !pending && <span className="setting-when">сразу</span>}
        {control}
      </div>
    </div>
  );
}

function Connection({ overview }) {
  const rows = [['OpenAI-совместимый адрес', overview?.connection?.openai_base_url], ['Anthropic-совместимый адрес', overview?.connection?.anthropic_base_url]];
  return (
    <section className="panel connect" aria-label="Подключение IDE">
      <div className="panel-head">
        <h2>Подключение IDE</h2>
        <span className="status"><Dot tone={overview?.session?.error ? 'neg' : 'pos'} />
          {overview?.session?.error ? 'сессия Retool с ошибкой' : 'сессия Retool активна'}</span>
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

function Catalog({ apiKey, onUnauthorized }) {
  const [data, setData] = useState(null);
  const [query, setQuery] = useState('');
  const [onlyRoutable, setOnlyRoutable] = useState(false);
  const [expanded, setExpanded] = useState(false);
  usePolling(async () => {
    try { setData(await api(apiKey, 'models')); } catch (failure) { if (failure.status === 401) onUnauthorized(); }
  }, 60000, [apiKey]);
  const all = data?.data || [];
  const items = all.filter((model) => (!onlyRoutable || model.routable)
    && (!query || `${model.id} ${model.label} ${model.provider}`.toLowerCase().includes(query.toLowerCase())));
  // The default model leads; a long catalog is folded unless searched or expanded.
  items.sort((a, b) => (b.id === data?.default_model) - (a.id === data?.default_model));
  const shown = expanded || query ? items : items.slice(0, CATALOG_FOLD);
  return (
    <section className="panel model-table" aria-label="Каталог моделей">
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
      {shown.map((model) => (
        <article key={model.id} className={`model-card ${model.id === data?.default_model ? 'is-default' : ''}`}>
          <span className="model-name"><strong>{model.label}</strong><code>{model.id}</code></span>
          <span className="model-provider">{model.provider}</span>
          <span className="num">{fmtTokens(model.context)}</span>
          <span className="model-effort">{model.reasoning ? (model.reasoning_effort_levels || []).join(' · ') || 'есть' : 'нет'}</span>
          <span className="model-route">
            <Dot tone={model.routable ? 'pos' : 'muted'} />{model.routable ? 'маршрут есть' : 'нет ресурса'}
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

export default function SettingsView({ apiKey, overview, onUnauthorized }) {
  const toast = useToast();
  const [state, setState] = useState(null);
  const [draft, setDraft] = useState({});
  const [saving, setSaving] = useState(false);

  usePolling(async () => {
    try {
      setState(await api(apiKey, 'settings'));
    } catch (failure) {
      if (failure.status === 401) onUnauthorized();
    }
  }, 30000, [apiKey]);

  const changes = useMemo(() => Object.fromEntries(Object.entries(draft)
    .filter(([key, value]) => state && value !== state.settings[key] && value !== '')), [draft, state]);
  const dirty = Object.keys(changes).length;

  async function save() {
    setSaving(true);
    try {
      const result = await api(apiKey, 'settings', { method: 'PATCH', body: { settings: changes } });
      setState(result);
      setDraft({});
      toast({
        tone: 'success', title: 'Настройки сохранены',
        text: result.applied.length ? `Применено сразу: ${result.applied.join(', ')}` : 'Изменения вступят в силу после перезапуска relay',
      });
    } catch (failure) {
      if (failure.status === 401) onUnauthorized();
      else toast({ tone: 'error', title: 'Не сохранено', text: failure.message });
    } finally {
      setSaving(false);
    }
  }

  const schema = state?.schema || {};
  const byGroup = {};
  for (const [key, field] of Object.entries(schema)) (byGroup[groupOf(field)] ||= []).push(key);

  return (
    <Page className="settings">
      <header className="page-head">
        <div>
          <h1>Настройки</h1>
          <p className="page-meta">записываются в config.json и применяются после перезапуска relay, кроме помеченных «сразу» · подключение IDE и каталог моделей внизу</p>
        </div>
        <div className="head-actions">
          {state?.restart_required && <Tag tone="warn">ждут перезапуска: {state.pending_restart.length}</Tag>}
        </div>
      </header>
      {state && !state.writable && <div className="banner tone-warn">Файл конфигурации недоступен для записи</div>}
      {state && GROUPS.map((group) => {
        const keys = byGroup[group.id] || [];
        if (!keys.length) return null;
        return (
          <section key={group.id} className="panel settings-group" aria-label={group.title}>
            <h2>{group.title}</h2>
            <p className="settings-hint">{group.hint}</p>
            {keys.map((key) => (
              <Field key={key} name={key} field={schema[key]}
                value={key in draft ? draft[key] : state.settings[key]}
                pending={state.pending_restart.includes(key)}
                dirty={key in changes}
                onChange={(value) => setDraft((d) => ({ ...d, [key]: value }))} />
            ))}
          </section>
        );
      })}
      <Connection overview={overview} />
      <Catalog apiKey={apiKey} onUnauthorized={onUnauthorized} />
      <AnimatePresence>
        {dirty > 0 && (
          <motion.div className="savebar" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 8 }}
            transition={{ duration: 0.2, ease }}>
            <span><b className="num">{dirty}</b> несохранённых: <span className="savebar-keys">{Object.keys(changes).map((k) => schema[k]?.label || k).join(', ')}</span></span>
            <Button icon={RotateCcw} onClick={() => setDraft({})}>Отменить</Button>
            <Button icon={Save} tone="primary" onClick={save} disabled={saving}>{saving ? 'Сохранение' : 'Сохранить'}</Button>
          </motion.div>
        )}
      </AnimatePresence>
    </Page>
  );
}
