import { useState } from 'react';
import { Check, KeyRound, Play, Plus, RefreshCw, Route as RouteIcon, Server, Trash, Wrench, X } from 'lucide-react';
import { api } from '../api.js';
import { usePolling } from '../useJournal.js';
import { Dot } from '../motion.jsx';
import { Button, CopyButton, Tag, Toggle, useToast } from '../ui.jsx';
import { fmtFixed, fmtInt, isNum } from '../format.js';
import { EMPTY_PROVIDER, buildProviderPayload, presetMatchesUrl, providerToForm } from '../providerForm.js';

const DRIVER_LABELS = {
  'openai-chat': 'OpenAI Chat Completions',
  'openai-responses': 'OpenAI Responses API',
  'anthropic-messages': 'Anthropic Messages API',
};

const AUTH_LABELS = {
  bearer: 'Authorization: Bearer',
  'x-api-key': 'x-api-key',
  'custom-header': 'Свой HTTP-заголовок',
  none: 'Без ключа (локальный LLM)',
};

const STRATEGY_LABELS = {
  'fill-first': 'fill-first (основной → резерв ради кэша)',
  weighted: 'weighted (пропорционально весу)',
  'least-inflight': 'least-inflight (наименее занятый)',
};

const ROUTE_STRATEGY_LABELS = {
  priority: 'priority (по порядку цепочки)',
  weighted: 'weighted (равномерно / по весу)',
  'least-inflight': 'least-inflight (наименее занятый)',
};

// What a save or a refresh of /models says beyond "done": the error of a provider whose /models did not answer, and a
// restart the relay asks for when it could not take the provider in live (the answer carries both; they were dropped).
function discoveryNote(result, pid) {
  const info = result?.discovery?.providers?.[pid];
  const notes = [];
  if (info && info.ok === false) notes.push(`/models не прочитан: ${info.error || 'ошибка запроса'}`);
  if (result?.restart_required) notes.push('подключится после перезапуска relay');
  return notes.join(' · ');
}

function ProviderForm({ presets, editing, onCancel, onSaved, apiKey, onUnauthorized }) {
  const toast = useToast();
  const [form, setForm] = useState(() => (editing ? providerToForm(editing) : EMPTY_PROVIDER));
  const [formError, setFormError] = useState('');
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [probe, setProbe] = useState(null);

  const set = (key) => (e) => {
    const val = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
    setForm((prev) => {
      const next = { ...prev, [key]: val };
      // A base URL moved off the preset's host is a provider of its own: the preset (and its balance endpoint, which
      // would get this provider's key) no longer applies.
      if (key === 'base_url' && prev.preset) {
        const spec = presets.find((p) => p.id === prev.preset);
        if (spec && !presetMatchesUrl(spec, val)) next.preset = '';
      }
      return next;
    });
    setFormError('');
  };

  function choosePreset(presetId) {
    if (!presetId) {
      setForm((prev) => ({ ...prev, preset: '', name: prev.name || 'Custom API', id: editing ? prev.id : 'custom' }));
      return;
    }
    const spec = presets.find((p) => p.id === presetId);
    if (!spec) return;
    setForm((prev) => ({
      ...prev,
      preset: spec.id,
      name: editing ? prev.name : spec.name,
      id: editing ? prev.id : spec.id,
      driver: spec.driver,
      base_url: spec.base_url,
      auth_mode: spec.auth_mode,
      cache_policy: spec.cache_policy,
    }));
    setProbe(null);
    setFormError('');
  }

  async function runTest() {
    setFormError('');
    setProbe(null);
    let payload;
    try {
      payload = buildProviderPayload(form, editing?.id || null, presets);
      if (editing?.id) payload.provider_id = editing.id;
    } catch (err) {
      setFormError(err.message);
      return;
    }
    setTesting(true);
    try {
      const res = await api(apiKey, 'providers/test', { method: 'POST', body: payload });
      setProbe(res);
    } catch (failure) {
      if (failure.status === 401) onUnauthorized();
      else setFormError(failure.message);
    } finally {
      setTesting(false);
    }
  }

  async function submit(e) {
    e.preventDefault();
    setFormError('');
    let payload;
    try {
      payload = buildProviderPayload(form, editing?.id || null, presets);
    } catch (err) {
      setFormError(err.message);
      return;
    }
    setSaving(true);
    try {
      const result = editing
        ? await api(apiKey, `providers/${editing.id}`, { method: 'PATCH', body: payload })
        : await api(apiKey, 'providers', { method: 'POST', body: payload });
      const note = discoveryNote(result, result.provider.id);
      toast({
        tone: note ? 'warn' : 'success',
        title: editing ? 'Провайдер обновлён' : 'Провайдер подключён',
        text: `${result.provider.name}: моделей в каталоге — ${fmtInt(result.provider.published_models_count)}${note ? ` · ${note}` : ''}`,
      });
      onSaved();
    } catch (failure) {
      if (failure.status === 401) onUnauthorized();
      else setFormError(failure.message);
    } finally {
      setSaving(false);
    }
  }

  const hasKeysText = form.api_keys_text.trim().length > 0;
  const hasEnv = form.api_key_env.trim().length > 0;

  return (
    <form className="gw-form" onSubmit={submit}>
      <div className="panel-head">
        <h3>{editing ? `Настройка провайдера ${editing.name}` : 'Подключить универсального провайдера'}</h3>
        <button type="button" className="icon-button ghost" aria-label="Закрыть форму провайдера" onClick={onCancel}>
          <X size={16} strokeWidth={1.5} />
        </button>
      </div>

      {!editing && presets.length > 0 && (
        <div className="chips" role="group" aria-label="Пресеты провайдеров">
          {presets.map((p) => (
            <button key={p.id} type="button" className={`chip ${form.preset === p.id ? 'on' : ''}`}
              aria-pressed={form.preset === p.id} onClick={() => choosePreset(p.id)}>
              {p.name}
            </button>
          ))}
          <button type="button" className={`chip ${form.preset === '' ? 'on' : ''}`}
            aria-pressed={form.preset === ''} onClick={() => choosePreset('')}>
            Свой URL
          </button>
        </div>
      )}

      <div className="form-grid">
        <label className="field">
          <span>Название</span>
          <input value={form.name} onChange={set('name')} maxLength={80} placeholder="OpenRouter" required />
        </label>
        <label className="field">
          <span>ID провайдера (префикс моделей)</span>
          <input value={form.id} onChange={set('id')} disabled={!!editing} placeholder="openrouter" spellCheck={false} required />
        </label>
        <label className="field">
          <span>Протокол (драйвер)</span>
          <select aria-label="Протокол (драйвер)" value={form.driver} onChange={set('driver')}>
            {Object.entries(DRIVER_LABELS).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select>
        </label>
        <label className="field">
          <span>Base URL</span>
          <input value={form.base_url} onChange={set('base_url')} placeholder="https://openrouter.ai/api/v1" spellCheck={false} required />
        </label>
        <label className="field">
          <span>Авторизация</span>
          <select aria-label="Авторизация" value={form.auth_mode} onChange={set('auth_mode')}>
            {Object.entries(AUTH_LABELS).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select>
        </label>
        {form.auth_mode === 'custom-header' && (
          <label className="field">
            <span>Имя HTTP-заголовка ключа</span>
            <input value={form.auth_header_name} onChange={set('auth_header_name')} placeholder="X-Custom-Key" spellCheck={false} required />
          </label>
        )}
        <label className="field">
          <span>Стратегия пула ключей</span>
          <select aria-label="Стратегия пула ключей" value={form.routing_strategy} onChange={set('routing_strategy')}>
            {Object.entries(STRATEGY_LABELS).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select>
        </label>
        <label className="field">
          <span>Политика кэша промпта</span>
          <select aria-label="Политика кэша промпта" value={form.cache_policy} onChange={set('cache_policy')}>
            <option value="auto">auto (умный кэш по модели)</option>
            <option value="5m">5m (стандартный TTL)</option>
            <option value="1h">1h (расширенный TTL Anthropic)</option>
            <option value="off">off (без маркеров кэша)</option>
          </select>
        </label>

        {form.auth_mode !== 'none' && (
          <>
            <label className="field field-wide">
              <span>{editing ? 'Новые API-ключи (оставьте пустым, чтобы сохранить текущие)' : 'API-ключи (один или пул по одному на строку)'}</span>
              <textarea className="token-lines" rows={3} value={form.api_keys_text} onChange={set('api_keys_text')}
                disabled={hasEnv} spellCheck={false} autoComplete="off" placeholder={editing ? 'Ключ уже сохранён в config.json' : 'sk-...'} />
            </label>
            <label className="field field-wide">
              <span>Или имя переменной окружения с ключом</span>
              <input value={form.api_key_env} onChange={set('api_key_env')} disabled={hasKeysText}
                placeholder="OPENROUTER_API_KEY" spellCheck={false} autoComplete="off" />
            </label>
          </>
        )}
      </div>

      <label className="check">
        <input type="checkbox" checked={form.auto_discover} onChange={set('auto_discover')} />
        <span>Автоматически загружать и обновлять список моделей с /models</span>
      </label>

      <details className="gw-details">
        <summary>Расширенные настройки (ручные модели, фильтры, прокси, заголовки)</summary>
        <div className="form-grid gw-details-body">
          <label className="field field-wide">
            <span>Ручные модели (ID через запятую, JSON-массив или объект OpenCode)</span>
            <input value={form.models_text} onChange={set('models_text')} placeholder="deepseek/deepseek-chat-v3, anthropic/claude-sonnet-4-5" spellCheck={false} />
          </label>
          <label className="field">
            <span>Белый список моделей (glob через запятую)</span>
            <input value={form.whitelist_text} onChange={set('whitelist_text')} placeholder="anthropic/*, deepseek/*" spellCheck={false} />
          </label>
          <label className="field">
            <span>Чёрный список моделей (glob через запятую)</span>
            <input value={form.blacklist_text} onChange={set('blacklist_text')} placeholder="*:free" spellCheck={false} />
          </label>
          <label className="field field-wide">
            <span>Прокси (HTTP / SOCKS5, необязательно)</span>
            <input value={form.proxy_url} onChange={set('proxy_url')} placeholder="socks5://127.0.0.1:1080" spellCheck={false} />
          </label>
          <label className="field">
            <span>{form.saved_headers?.length
              ? `Доп. HTTP-заголовки (JSON): добавить к сохранённым ${form.saved_headers.join(', ')}; null удаляет`
              : 'Доп. HTTP-заголовки (JSON)'}</span>
            <textarea rows={2} value={form.headers_text} onChange={set('headers_text')} placeholder={'{"HTTP-Referer": "https://localhost"}'} spellCheck={false} />
          </label>
          <label className="field">
            <span>Body overrides (JSON)</span>
            <textarea rows={2} value={form.body_overrides_text} onChange={set('body_overrides_text')} placeholder={'{"service_tier": "auto"}'} spellCheck={false} />
          </label>
        </div>
      </details>

      {formError && <p className="banner tone-neg" role="alert">{formError}</p>}

      {probe && (
        <div className={`banner ${probe.ok ? 'tone-pos-box' : 'tone-neg'}`} role="status">
          <span>
            {probe.ok
              ? `Соединение проверено (${fmtInt(probe.latency_ms)} мс) · доступно моделей на /models: ${fmtInt(probe.models_count)}`
              : `Проверка не прошла (${fmtInt(probe.latency_ms)} мс): ${probe.error}`}
          </span>
        </div>
      )}

      <div className="form-actions">
        <Button icon={Play} onClick={runTest} disabled={testing || saving}>
          {testing ? 'Проверка…' : 'Проверить соединение'}
        </Button>
        <Button onClick={onCancel}>Отмена</Button>
        <Button tone="primary" type="submit" disabled={saving}>
          {saving ? 'Сохранение…' : (editing ? 'Сохранить' : 'Подключить')}
        </Button>
      </div>
    </form>
  );
}

export default function GatewayPanels({ apiKey, onUnauthorized, refreshOverview }) {
  const toast = useToast();
  const [provData, setProvData] = useState(null);
  const [routesData, setRoutesData] = useState(null);
  const [keysData, setKeysData] = useState(null);
  const [busy, setBusy] = useState({});

  const [provFormOpen, setProvFormOpen] = useState(false);
  const [editingProv, setEditingProv] = useState(null);

  const [routeFormOpen, setRouteFormOpen] = useState(false);
  const [routeDraft, setRouteDraft] = useState({ id: '', label: '', targets_text: '', strategy: 'priority', editing: false });
  const [routeError, setRouteError] = useState('');
  const [pingResult, setPingResult] = useState({});

  const [keyFormOpen, setKeyFormOpen] = useState(false);
  const [keyDraft, setKeyDraft] = useState({ label: '', allowed_text: '', rpm_limit: '', max_budget_usd: '' });
  const [keyError, setKeyError] = useState('');
  const [createdRawKey, setCreatedRawKey] = useState(null);

  async function loadAll() {
    try {
      const [p, r, k] = await Promise.all([
        api(apiKey, 'providers').catch((err) => { if (err.status === 401) throw err; return null; }),
        api(apiKey, 'routes').catch((err) => { if (err.status === 401) throw err; return null; }),
        api(apiKey, 'client-keys').catch((err) => { if (err.status === 401) throw err; return null; }),
      ]);
      if (p) setProvData(p);
      if (r) setRoutesData(r);
      if (k) setKeysData(k);
    } catch (failure) {
      if (failure.status === 401) onUnauthorized();
    }
  }
  usePolling(loadAll, 12000, [apiKey]);

  // If backend mock does not serve gateway endpoints at all, stay out of the way cleanly.
  if (!provData && !routesData && !keysData) return null;

  const providers = provData?.providers || [];
  const presets = provData?.presets || [];
  const routes = routesData?.routes || [];
  const clientKeys = keysData?.client_keys || [];

  async function withBusy(id, fn) {
    setBusy((b) => ({ ...b, [id]: true }));
    try {
      await fn();
      await loadAll();
      refreshOverview?.();
    } catch (failure) {
      if (failure.status === 401) onUnauthorized();
      else toast({ tone: 'error', title: 'Ошибка операции', text: failure.message });
    } finally {
      setBusy((b) => ({ ...b, [id]: false }));
    }
  }

  async function pingModel(modelId) {
    setBusy((b) => ({ ...b, [`ping:${modelId}`]: true }));
    try {
      const res = await api(apiKey, 'models/ping', { method: 'POST', body: { model: modelId } });
      setPingResult((prev) => ({ ...prev, [modelId]: res }));
      toast(res.ok
        ? { tone: 'success', title: `Пинг ${modelId}: OK`, text: `${fmtInt(res.latency_ms)} мс · ответ: ${res.reply}` }
        : { tone: 'error', title: `Пинг ${modelId}: ошибка`, text: res.error });
    } catch (failure) {
      if (failure.status === 401) onUnauthorized();
      else toast({ tone: 'error', title: 'Ошибка пинга', text: failure.message });
    } finally {
      setBusy((b) => ({ ...b, [`ping:${modelId}`]: false }));
    }
  }

  async function saveRoute(e) {
    e.preventDefault();
    setRouteError('');
    const id = routeDraft.id.trim();
    const targets = routeDraft.targets_text.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);
    if (!id || !targets.length) {
      setRouteError('Укажите алиас маршрута и хотя бы одну целевую модель');
      return;
    }
    const body = { id, label: routeDraft.label.trim() || id, targets, strategy: routeDraft.strategy };
    await withBusy('save-route', async () => {
      if (routeDraft.editing) await api(apiKey, `routes/${encodeURIComponent(id)}`, { method: 'PATCH', body });
      else await api(apiKey, 'routes', { method: 'POST', body });
      setRouteFormOpen(false);
      setRouteDraft({ id: '', label: '', targets_text: '', strategy: 'priority', editing: false });
      toast({ tone: 'success', title: routeDraft.editing ? 'Маршрут обновлён' : 'Маршрут создан', text: id });
    });
  }

  async function saveClientKey(e) {
    e.preventDefault();
    setKeyError('');
    const label = keyDraft.label.trim();
    if (!label) {
      setKeyError('Укажите название клиента или устройства');
      return;
    }
    const allowed = keyDraft.allowed_text.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);
    const body = {
      label,
      allowed_models: allowed.length ? allowed : null,
      rpm_limit: keyDraft.rpm_limit !== '' ? parseInt(keyDraft.rpm_limit, 10) : null,
      max_budget_usd: keyDraft.max_budget_usd !== '' ? parseFloat(keyDraft.max_budget_usd) : null,
    };
    await withBusy('save-ckey', async () => {
      const res = await api(apiKey, 'client-keys', { method: 'POST', body });
      setCreatedRawKey({ label, key: res.raw_key });
      setKeyFormOpen(false);
      setKeyDraft({ label: '', allowed_text: '', rpm_limit: '', max_budget_usd: '' });
      toast({ tone: 'success', title: 'Клиентский ключ создан', text: 'Скопируйте ключ — целиком он показывается один раз' });
    });
  }

  return (
    <div className="gw-panel stack">
      {/* 1. Universal Providers */}
      <section className="panel" aria-label="Универсальные провайдеры">
        <div className="panel-head">
          <div>
            <h2><Server size={16} strokeWidth={1.5} aria-hidden="true" /> Универсальные провайдеры</h2>
            <p className="panel-meta">
              подключено {fmtInt(providers.length)} · моделей опубликовано {fmtInt(providers.reduce((s, p) => s + (p.published_models_count || 0), 0))}
            </p>
          </div>
          <div className="head-actions">
            {providers.length > 0 && (
              <Button icon={RefreshCw} disabled={!!busy.refreshAll}
                onClick={() => withBusy('refreshAll', async () => {
                  const res = await api(apiKey, 'catalog/refresh', { method: 'POST', body: {} });
                  const failed = Object.entries(res.providers || {}).filter(([, info]) => info && info.ok === false);
                  toast(!res.ok
                    ? { tone: 'warn', title: 'Каталог обновляется', text: res.error || 'Обновление ещё идёт' }
                    : failed.length
                      ? { tone: 'warn', title: 'Каталог обновлён не полностью', text: failed.map(([pid, info]) => `${pid}: ${info.error || 'ошибка'}`).join(' · ') }
                      : { tone: 'success', title: 'Каталог обновлён', text: `Всего моделей провайдеров: ${fmtInt(res.total_models)}` });
                })}>
                Обновить /models
              </Button>
            )}
            <Button icon={Plus} tone={providers.length ? 'default' : 'primary'}
              disabled={provFormOpen && !editingProv}
              onClick={() => { setEditingProv(null); setProvFormOpen(true); }}>
              Подключить провайдера
            </Button>
          </div>
        </div>

        {(provFormOpen || editingProv) && (
          <ProviderForm
            key={editingProv ? editingProv.id : '_new'}
            presets={presets}
            editing={editingProv}
            apiKey={apiKey}
            onUnauthorized={onUnauthorized}
            onCancel={() => { setProvFormOpen(false); setEditingProv(null); }}
            onSaved={() => { setProvFormOpen(false); setEditingProv(null); loadAll(); refreshOverview?.(); }}
          />
        )}

        {providers.length > 0 ? (
          <ul className="gw-list">
            {providers.map((prov) => (
              <li key={prov.id} className={`gw-row${prov.enabled === false ? ' is-off' : ''}`}>
                <div className="gw-main">
                  <Dot tone={!prov.enabled ? 'muted' : prov.routable ? 'pos' : 'warn'} />
                  <div className="gw-title">
                    <strong>{prov.name}</strong>
                    <code>{prov.id}/…</code>
                    <Tag>{prov.driver}</Tag>
                    <Tag tone={prov.cache_policy === 'off' ? 'muted' : 'accent'}>кэш: {prov.cache_policy}</Tag>
                  </div>
                </div>
                <div className="gw-meta mono">
                  <span>{prov.base_url}</span>
                  <span>
                    моделей: <b>{fmtInt(prov.published_models_count)}</b>
                    {prov.credentials_count > 1 && ` · ключей: ${fmtInt(prov.credentials_count)} (${prov.routing_strategy})`}
                    {isNum(prov.available_usd) && <> · баланс: <b>${fmtFixed(prov.available_usd, 2)}</b></>}
                    {prov.turns > 0 && <> · ходов: {fmtInt(prov.turns)} (${fmtFixed(prov.cost_usd, 4)})</>}
                  </span>
                  {prov.error && <span className="neg-text">{prov.error}</span>}
                </div>
                <div className="gw-actions">
                  <Toggle
                    id={`prov-enabled-${prov.id}`}
                    checked={prov.enabled !== false}
                    label={`Провайдер ${prov.name} в маршрутизации`}
                    disabled={!!busy[`prov:${prov.id}`]}
                    onChange={(enabled) => withBusy(`prov:${prov.id}`, () => api(apiKey, `providers/${prov.id}`, { method: 'PATCH', body: { enabled } }))}
                  />
                  <Button icon={RefreshCw} disabled={!!busy[`disc:${prov.id}`] || prov.enabled === false}
                    title={prov.enabled === false ? 'Провайдер выключен: его /models не читается' : undefined}
                    onClick={() => withBusy(`disc:${prov.id}`, async () => {
                      const res = await api(apiKey, `providers/${prov.id}/discover`, { method: 'POST', body: {} });
                      const info = res.providers?.[prov.id];
                      toast(info?.ok
                        ? { tone: 'success', title: `${prov.name}: /models обновлён`, text: `Найдено моделей: ${fmtInt(info.count)}` }
                        : { tone: 'warn', title: `${prov.name}: не удалось обновить`, text: info?.error || res.error || 'Ошибка запроса' });
                    })}>
                    Модели
                  </Button>
                  <Button icon={Wrench} onClick={() => { setProvFormOpen(false); setEditingProv(prov); }}>
                    Изменить
                  </Button>
                  <button type="button" className="icon-button tone-danger" aria-label={`Удалить провайдера ${prov.name}`}
                    disabled={!!busy[`del:${prov.id}`]}
                    onClick={() => {
                      if (!window.confirm(`Удалить провайдера ${prov.name} (${prov.id}) из config.json?`)) return;
                      withBusy(`del:${prov.id}`, () => api(apiKey, `providers/${prov.id}`, { method: 'DELETE', body: {} }));
                    }}>
                    <Trash size={16} strokeWidth={1.5} />
                  </button>
                </div>
              </li>
            ))}
          </ul>
        ) : !provFormOpen && (
          <p className="muted small">
            Подключите OpenRouter, Anthropic, OpenAI, DeepSeek, Gemini, Ollama, vLLM или любой совместимый Base URL с автообновлением моделей и умным кэшем.
          </p>
        )}
      </section>

      {/* 2. Route Aliases & Fallback Chains */}
      <section className="panel" aria-label="Маршруты и фолбэки">
        <div className="panel-head">
          <div>
            <h2><RouteIcon size={16} strokeWidth={1.5} aria-hidden="true" /> Маршруты и фолбэки</h2>
            <p className="panel-meta">виртуальные имена моделей с автоматическим переключением между провайдерами</p>
          </div>
          <Button icon={Plus} disabled={routeFormOpen}
            onClick={() => {
              setRouteDraft({ id: '', label: '', targets_text: '', strategy: 'priority', editing: false });
              setRouteError('');
              setRouteFormOpen(true);
            }}>
            Новый маршрут
          </Button>
        </div>

        {routeFormOpen && (
          <form className="gw-form" onSubmit={saveRoute}>
            <div className="form-grid">
              <label className="field">
                <span>Имя алиаса (модель для IDE)</span>
                <input value={routeDraft.id} disabled={routeDraft.editing}
                  onChange={(e) => setRouteDraft((d) => ({ ...d, id: e.target.value }))}
                  placeholder="coding-best" spellCheck={false} required />
              </label>
              <label className="field">
                <span>Стратегия выбора</span>
                <select aria-label="Стратегия маршрута" value={routeDraft.strategy}
                  onChange={(e) => setRouteDraft((d) => ({ ...d, strategy: e.target.value }))}>
                  {Object.entries(ROUTE_STRATEGY_LABELS).map(([id, label]) => <option key={id} value={id}>{label}</option>)}
                </select>
              </label>
              <label className="field field-wide">
                <span>Целевые модели по порядку фолбэка (через запятую или с новой строки)</span>
                <textarea rows={2} value={routeDraft.targets_text}
                  onChange={(e) => setRouteDraft((d) => ({ ...d, targets_text: e.target.value }))}
                  placeholder="anthropic/claude-sonnet-4-5, openrouter/deepseek/deepseek-chat-v3" spellCheck={false} required />
              </label>
            </div>
            {routeError && <p className="banner tone-neg" role="alert">{routeError}</p>}
            <div className="form-actions">
              <Button onClick={() => setRouteFormOpen(false)}>Отмена</Button>
              <Button tone="primary" type="submit" disabled={!!busy['save-route']}>Сохранить маршрут</Button>
            </div>
          </form>
        )}

        {routes.length > 0 ? (
          <ul className="gw-list">
            {routes.map((route) => {
              const ping = pingResult[route.id];
              return (
                <li key={route.id} className="gw-row">
                  <div className="gw-main">
                    <Dot tone={route.routable ? 'pos' : 'warn'} />
                    <div className="gw-title">
                      <strong>{route.id}</strong>
                      <Tag>{route.strategy}</Tag>
                      {ping && <Tag tone={ping.ok ? 'pos' : 'neg'}>{ping.ok ? `OK · ${fmtInt(ping.latency_ms)} мс` : 'ошибка'}</Tag>}
                    </div>
                  </div>
                  <div className="gw-meta mono">
                    <span>
                      {(route.target_status || []).map((t, idx) => (
                        <span key={t.id}>
                          {idx > 0 && ' → '}
                          <span className={t.routable ? '' : 'muted'}>{t.id}</span>
                        </span>
                      ))}
                    </span>
                  </div>
                  <div className="gw-actions">
                    <Button icon={Play} disabled={!!busy[`ping:${route.id}`] || !route.routable}
                      onClick={() => pingModel(route.id)}>
                      Пинг
                    </Button>
                    <Button icon={Wrench} onClick={() => {
                      setRouteDraft({
                        id: route.id,
                        label: route.label || route.id,
                        targets_text: (route.targets || []).join(', '),
                        strategy: route.strategy || 'priority',
                        editing: true,
                      });
                      setRouteFormOpen(true);
                    }}>
                      Изменить
                    </Button>
                    <button type="button" className="icon-button tone-danger" aria-label={`Удалить маршрут ${route.id}`}
                      onClick={() => {
                        if (!window.confirm(`Удалить маршрут-алиас ${route.id}?`)) return;
                        withBusy(`del-route:${route.id}`, () => api(apiKey, `routes/${encodeURIComponent(route.id)}`, { method: 'DELETE', body: {} }));
                      }}>
                      <Trash size={16} strokeWidth={1.5} />
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        ) : !routeFormOpen && (
          <p className="muted small">Маршруты позволяют задать единое имя модели (например, <code>coding-best</code>) с цепочкой резервных провайдеров.</p>
        )}
      </section>

      {/* 3. Virtual Client Keys (AccessKeys) */}
      <section className="panel" aria-label="Клиентские ключи">
        <div className="panel-head">
          <div>
            <h2><KeyRound size={16} strokeWidth={1.5} aria-hidden="true" /> Клиентские ключи (AccessKeys)</h2>
            <p className="panel-meta">отдельные ключи для IDE и устройств с лимитами RPM, бюджетом USD и фильтром моделей</p>
          </div>
          <Button icon={Plus} disabled={keyFormOpen} onClick={() => { setKeyError(''); setKeyFormOpen(true); }}>
            Создать ключ
          </Button>
        </div>

        {createdRawKey && (
          <div className="banner tone-pos-box gw-key-banner" role="status">
            <div>
              <strong>Ключ «{createdRawKey.label}» создан (показывается один раз):</strong>{' '}
              <code>{createdRawKey.key}</code>
            </div>
            <div className="gw-actions">
              <CopyButton value={createdRawKey.key} label="Скопировать новый клиентский ключ" />
              <button type="button" className="icon-button ghost" aria-label="Скрыть созданный ключ" onClick={() => setCreatedRawKey(null)}>
                <Check size={16} strokeWidth={1.5} />
              </button>
            </div>
          </div>
        )}

        {keyFormOpen && (
          <form className="gw-form" onSubmit={saveClientKey}>
            <div className="form-grid">
              <label className="field">
                <span>Название клиента / устройства</span>
                <input value={keyDraft.label} onChange={(e) => setKeyDraft((d) => ({ ...d, label: e.target.value }))}
                  placeholder="Cursor рабочий ноутбук" maxLength={64} required />
              </label>
              <label className="field">
                <span>Разрешённые модели (glob через запятую, пусто — все)</span>
                <input value={keyDraft.allowed_text} onChange={(e) => setKeyDraft((d) => ({ ...d, allowed_text: e.target.value }))}
                  placeholder="coding-best, openrouter/*" spellCheck={false} />
              </label>
              <label className="field">
                <span>Лимит запросов в минуту (RPM, необязательно)</span>
                <input type="number" min="1" max="100000" value={keyDraft.rpm_limit}
                  onChange={(e) => setKeyDraft((d) => ({ ...d, rpm_limit: e.target.value }))} placeholder="60" />
              </label>
              <label className="field">
                <span>Бюджет в USD (необязательно): ходы Minara и API-провайдеров с запуска relay, ходы Retool не входят</span>
                <input type="number" min="0" step="0.01" value={keyDraft.max_budget_usd}
                  onChange={(e) => setKeyDraft((d) => ({ ...d, max_budget_usd: e.target.value }))} placeholder="10.00" />
              </label>
            </div>
            {keyError && <p className="banner tone-neg" role="alert">{keyError}</p>}
            <div className="form-actions">
              <Button onClick={() => setKeyFormOpen(false)}>Отмена</Button>
              <Button tone="primary" type="submit" disabled={!!busy['save-ckey']}>Выпустить ключ</Button>
            </div>
          </form>
        )}

        {clientKeys.length > 0 ? (
          <ul className="gw-list">
            {clientKeys.map((ck) => (
              <li key={ck.id} className={`gw-row${ck.enabled === false ? ' is-off' : ''}`}>
                <div className="gw-main">
                  <Dot tone={ck.enabled ? 'pos' : 'muted'} />
                  <div className="gw-title">
                    <strong>{ck.label}</strong>
                    <code>{ck.key_preview}</code>
                  </div>
                </div>
                <div className="gw-meta mono">
                  <span title="Счётчики с запуска relay. В долларах — ходы Minara и API-провайдеров; ходы Retool (его кредиты) в расход и бюджет ключа не входят">
                    запросов: <b>{fmtInt(ck.requests)}</b> · потрачено: <b>${fmtFixed(ck.spent_usd, 4)}</b>
                    {isNum(ck.max_budget_usd) && ` из $${fmtFixed(ck.max_budget_usd, 2)}`}
                    {isNum(ck.rpm_limit) && ` · лимит ${fmtInt(ck.rpm_limit)} RPM`}
                    {' · с запуска relay, без Retool'}
                  </span>
                  {ck.allowed_models?.length > 0 && <span>модели: {ck.allowed_models.join(', ')}</span>}
                </div>
                <div className="gw-actions">
                  <Toggle
                    id={`ckey-enabled-${ck.id}`}
                    checked={ck.enabled !== false}
                    label={`Клиентский ключ ${ck.label} активен`}
                    onChange={(enabled) => withBusy(`ckey:${ck.id}`, () => api(apiKey, `client-keys/${ck.id}`, { method: 'PATCH', body: { enabled } }))}
                  />
                  <button type="button" className="icon-button tone-danger" aria-label={`Удалить клиентский ключ ${ck.label}`}
                    onClick={() => {
                      if (!window.confirm(`Отозвать и удалить ключ «${ck.label}»?`)) return;
                      withBusy(`del-ckey:${ck.id}`, () => api(apiKey, `client-keys/${ck.id}`, { method: 'DELETE', body: {} }));
                    }}>
                    <Trash size={16} strokeWidth={1.5} />
                  </button>
                </div>
              </li>
            ))}
          </ul>
        ) : !keyFormOpen && (
          <p className="muted small">Основной <code>relay_api_key</code> управляет админкой; клиентские ключи дают доступ только к <code>/v1/*</code> с индивидуальными квотами.</p>
        )}
      </section>
    </div>
  );
}
