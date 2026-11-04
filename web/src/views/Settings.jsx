import { useMemo, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { RotateCcw, Save } from 'lucide-react';
import { api } from '../api.js';
import { usePolling } from '../useJournal.js';
import { Page, ease } from '../motion.jsx';
import { Button, Tag, Toggle, useToast } from '../ui.jsx';

function Field({ name, field, value, onChange, pending, dirty }) {
  const id = `setting-${name}`;
  let control;
  if (field.type === 'boolean') {
    control = <Toggle id={id} checked={!!value} label={field.label} onChange={onChange} />;
  } else if (field.enum) {
    control = (
      <select id={id} value={value ?? ''} onChange={(e) => onChange(e.target.value)}>
        {field.enum.map((option) => <option key={option} value={option}>{option}</option>)}
      </select>
    );
  } else {
    control = (
      <input id={id} type="number" min={field.min} step={field.type === 'integer' ? 1 : 'any'} value={value ?? ''}
        onChange={(e) => {
          const raw = e.target.value;
          onChange(raw === '' ? '' : field.type === 'integer' ? parseInt(raw, 10) : parseFloat(raw));
        }} />
    );
  }
  return (
    <div className={`setting ${pending ? 'pending' : ''} ${dirty ? 'dirty' : ''}`}>
      <label htmlFor={id}>
        <strong>{field.label}</strong>
        <small>{field.description}</small>
      </label>
      <div className="setting-control">
        {pending && <Tag tone="warn" title="Сохранено, применится после перезапуска">после перезапуска</Tag>}
        {control}
      </div>
    </div>
  );
}

export default function SettingsView({ apiKey, onUnauthorized }) {
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
  const groups = [
    ['Модель и поведение', ['model', 'reasoning_effort', 'session_mode', 'exec_policy', 'show_thinking', 'project_context', 'show_exec_output', 'strict_models', 'show_unrouted_models', 'cache_keepwarm_during_turns']],
    ['API', ['enable_chat', 'enable_responses', 'enable_anthropic']],
    ['Лимиты и таймауты', Object.keys(schema).filter((key) => ['integer', 'number'].includes(schema[key].type))],
  ];

  return (
    <Page className="settings">
      <header className="page-head">
        <div>
          <h1>Настройки</h1>
          <p className="page-meta">config.json · изменения пишутся в файл relay</p>
        </div>
        <div className="head-actions">
          {state?.restart_required && <Tag tone="warn">ждут перезапуска: {state.pending_restart.length}</Tag>}
        </div>
      </header>
      {state && !state.writable && <div className="banner tone-warn">Файл конфигурации недоступен для записи</div>}
      {state && groups.map(([title, keys]) => {
        const present = keys.filter((key) => schema[key]);
        if (!present.length) return null;
        return (
          <section key={title} className="panel settings-group">
            <h2>{title}</h2>
            {present.map((key) => (
              <Field key={key} name={key} field={schema[key]}
                value={key in draft ? draft[key] : state.settings[key]}
                pending={state.pending_restart.includes(key)}
                dirty={key in changes}
                onChange={(value) => setDraft((d) => ({ ...d, [key]: value }))} />
            ))}
          </section>
        );
      })}
      <AnimatePresence>
        {dirty > 0 && (
          <motion.div className="savebar" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 8 }}
            transition={{ duration: 0.2, ease }}>
            <span><b className="num">{dirty}</b> несохранённых: <span className="savebar-keys">{Object.keys(changes).join(', ')}</span></span>
            <Button icon={RotateCcw} onClick={() => setDraft({})}>Отменить</Button>
            <Button icon={Save} tone="primary" onClick={save} disabled={saving}>{saving ? 'Сохранение' : 'Сохранить'}</Button>
          </motion.div>
        )}
      </AnimatePresence>
    </Page>
  );
}
