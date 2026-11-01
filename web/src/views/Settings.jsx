import { useMemo, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { RotateCcw, Save, TriangleAlert } from 'lucide-react';
import { api } from '../api.js';
import { usePolling } from '../useJournal.js';
import { Page, Reveal, spring } from '../motion.jsx';
import { Badge, Button, Toggle, useToast } from '../ui.jsx';

function Field({ name, field, value, onChange, pending }) {
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
    <motion.div className={`setting ${pending ? 'pending' : ''}`} layout variants={{ initial: { opacity: 0, y: 10 }, animate: { opacity: 1, y: 0 } }}>
      <label htmlFor={id}>
        <strong>{field.label}</strong>
        <small className="muted">{field.description}</small>
      </label>
      <div className="setting-control">
        {control}
        {pending && <Badge tone="warn" title="Сохранено, применится после перезапуска">перезапуск</Badge>}
        {!field.restart_required && <Badge tone="success">сразу</Badge>}
      </div>
    </motion.div>
  );
}

export default function SettingsView({ apiKey, onUnauthorized }) {
  const toast = useToast();
  const [state, setState] = useState(null);
  const [draft, setDraft] = useState({});
  const [saving, setSaving] = useState(false);

  usePolling(async () => {
    try {
      const data = await api(apiKey, 'settings');
      setState(data);
    } catch (failure) {
      if (failure.status === 401) onUnauthorized();
    }
  }, 30000, [apiKey]);

  const changes = useMemo(() => Object.fromEntries(Object.entries(draft).filter(([key, value]) => state && value !== state.settings[key] && value !== '')), [draft, state]);
  const dirty = Object.keys(changes).length;

  async function save() {
    setSaving(true);
    try {
      const result = await api(apiKey, 'settings', { method: 'PATCH', body: { settings: changes } });
      setState(result);
      setDraft({});
      toast({
        tone: 'success', title: 'Настройки сохранены',
        text: result.applied.length ? `Применено сразу: ${result.applied.join(', ')}` : 'Изменения вступят в силу после перезапуска relay.',
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
      <Reveal as="header" className="page-head">
        <div>
          <p className="eyebrow">Конфигурация · config.json</p>
          <h1>Настройки</h1>
        </div>
        <div className="head-actions">
          {state?.restart_required && <Badge tone="warn"><TriangleAlert size={13} /> ждут перезапуска: {state.pending_restart.length}</Badge>}
        </div>
      </Reveal>
      {state && !state.writable && <div className="banner tone-warn">Файл конфигурации недоступен для записи.</div>}
      {groups.map(([title, keys]) => (
        <Reveal key={title} className="panel settings-group">
          <h2>{title}</h2>
          <motion.div initial="initial" animate="animate" transition={{ staggerChildren: 0.03 }}>
            {keys.filter((key) => schema[key]).map((key) => (
              <Field key={key} name={key} field={schema[key]}
                value={key in draft ? draft[key] : state.settings[key]}
                pending={state.pending_restart.includes(key)}
                onChange={(value) => setDraft((d) => ({ ...d, [key]: value }))} />
            ))}
          </motion.div>
        </Reveal>
      ))}
      <AnimatePresence>
        {dirty > 0 && (
          <motion.div className="savebar" initial={{ y: 90, opacity: 0 }} animate={{ y: 0, opacity: 1 }} exit={{ y: 90, opacity: 0 }} transition={spring}>
            <span><b className="num">{dirty}</b> несохранённых изменений: {Object.keys(changes).join(', ')}</span>
            <Button icon={RotateCcw} onClick={() => setDraft({})}>Отменить</Button>
            <Button icon={Save} tone="primary" onClick={save} disabled={saving}>{saving ? 'Сохранение…' : 'Сохранить'}</Button>
          </motion.div>
        )}
      </AnimatePresence>
    </Page>
  );
}
