import { useState } from 'react';
import { Plus, RefreshCw, RotateCw, Trash, Wallet, X } from 'lucide-react';
import { api } from '../api.js';
import { usePolling } from '../useJournal.js';
import { Dot, Page } from '../motion.jsx';
import { Button, Empty, Tag, Toggle, useToast } from '../ui.jsx';
import { ACCOUNT_STATE } from '../catalog.js';
import { fmtAgo, fmtDay, fmtFixed, fmtInt, isNum, plural } from '../format.js';
import { PoolScene } from '../stage/PoolScene.jsx';
import { AddGuide, GuideNum } from '../stage/AddGuide.jsx';

const pct = (share) => `${Math.max(0, Math.min(1, share)) * 100}%`;

function Priority({ value, disabled, onSave }) {
  const [draft, setDraft] = useState(null);
  const shown = draft ?? String(value ?? 0);
  const commit = () => {
    if (draft === null) return;
    const next = Math.max(0, Math.min(99, parseInt(draft, 10) || 0));
    setDraft(null);
    if (next !== value) onSave(next);
  };
  return (
    <label className="priority-field"><span>приоритет</span><input className="priority" type="number" min="0" max="99" value={shown} disabled={disabled}
      aria-label="Приоритет: меньше — раньше" title="Приоритет: меньше — раньше"
      onChange={(e) => setDraft(e.target.value)} onBlur={commit}
      onKeyDown={(e) => { if (e.key === 'Enter') e.currentTarget.blur(); }} /></label>
  );
}

function AccountRow({ account, busy, onAction, onPatch, onDelete, lastEnabled }) {
  const state = account.enabled === false ? { label: 'выключен', tone: 'muted' }
    : ACCOUNT_STATE[account.state] || { label: account.state || '—', tone: 'muted' };
  const share = isNum(account.available) && account.monthly_allocation ? account.available / account.monthly_allocation : null;
  const name = account.label || account.account;
  const pending = account.pending === 'added' ? 'подключится после перезапуска'
    : account.pending === 'removed' ? 'удалён, исчезнет после перезапуска' : null;
  return (
    <li className={`pool-row${account.enabled === false ? ' is-off' : ''}`}>
      <div className="pool-name">
        <Dot tone={state.tone} />
        <span>
          <strong>{name}</strong>
          {account.label && <code>{account.account}</code>}
        </span>
      </div>
      <div className="pool-state">
        <span>{state.label}</span>
        {account.reserve && <Tag tone="warn" title="Все аккаунты ниже порога: этот обслуживает запросы из резерва">резерв</Tag>}
        {account.stale && <Tag tone="warn">устарело</Tag>}
        {pending && <Tag tone="accent">{pending}</Tag>}
      </div>
      <div className="pool-money">
        <span className="pool-money-row">
          <b className="num">{fmtFixed(account.available, 2)}</b>
          <span className="muted num">из {fmtInt(account.monthly_allocation)}</span>
        </span>
        <span className="acc-bar">{share !== null && <span style={{ width: pct(share) }} />}</span>
        <span className="pool-meta">
          сброс {fmtDay(account.reset_date)} · с запуска {fmtFixed(account.session_used, 2)} за {fmtInt(account.turns)} {plural(account.turns || 0, ['ход', 'хода', 'ходов'])}
        </span>
      </div>
      <div className="pool-health">
        <span>баланс: {account.last_balance_at ? fmtAgo(account.last_balance_at) : 'ещё не проверялся'}</span>
        <span>сессия: {account.last_renewed_at ? fmtAgo(account.last_renewed_at) : 'ещё не продлевалась'}</span>
        {account.auth_failures > 0 && <span className="neg-text">отказов входа подряд: {account.auth_failures}</span>}
        {account.error && <span className="neg-text" title={account.error}>{account.error}</span>}
      </div>
      <div className="pool-controls">
        <Priority value={account.priority} disabled={!!account.pending || busy} onSave={(priority) => onPatch({ priority })} />
        <Toggle id={`enabled-${account.account}`} checked={account.enabled !== false}
          label={`Аккаунт ${name} в маршрутизации`}
          onChange={(enabled) => (!enabled && lastEnabled ? null : onPatch({ enabled }))} />
      </div>
      <div className="pool-actions">
        <Button icon={Wallet} disabled={busy || !!account.pending} onClick={() => onAction('balance')}>Баланс</Button>
        <Button icon={RotateCw} disabled={busy || !!account.pending} onClick={() => onAction('renew')}>Продлить</Button>
        <button type="button" className="icon-button" aria-label={`Удалить аккаунт ${name}`} title="Удалить из config.json"
          disabled={busy || account.pending === 'removed'} onClick={onDelete}>
          <Trash size={16} strokeWidth={1.5} />
        </button>
      </div>
    </li>
  );
}

const EMPTY_FORM = { label: '', base: '', page_uuid: '', cookie: '', xsrf: '', provider_resource_name: '', openai_provider_resource_name: '' };
const REQUIRED = ['base', 'page_uuid', 'cookie', 'xsrf'];

function AddForm({ onCancel, onSubmit, busy }) {
  const [form, setForm] = useState(EMPTY_FORM);
  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));
  const ready = form.base.trim() && form.page_uuid.trim() && form.cookie.trim() && form.xsrf.trim();
  return (
    <form className="panel add-form" onSubmit={(e) => { e.preventDefault(); if (ready) onSubmit(form); }}>
      <div className="panel-head">
        <h2>Новый аккаунт</h2>
        <button type="button" className="icon-button ghost" aria-label="Закрыть форму" onClick={onCancel}><X size={16} strokeWidth={1.5} /></button>
      </div>
      <div className="add-layout">
        <div className="form-grid">
          <label className="field"><span><GuideNum n={1} />Адрес Retool</span>
            <input value={form.base} onChange={set('base')} placeholder="https://<tenant>.retool.com" spellCheck={false} /></label>
          <label className="field"><span><GuideNum n={2} />page_uuid приложения</span>
            <input value={form.page_uuid} onChange={set('page_uuid')} spellCheck={false} /></label>
          <label className="field field-wide"><span><GuideNum n={3} />Cookie</span>
            <textarea value={form.cookie} onChange={set('cookie')} rows={3} spellCheck={false} autoComplete="off" /></label>
          <label className="field"><span><GuideNum n={4} />xsrf</span>
            <input value={form.xsrf} onChange={set('xsrf')} type="password" spellCheck={false} autoComplete="off" /></label>
          <label className="field"><span>Название</span>
            <input value={form.label} onChange={set('label')} maxLength={40} placeholder="например, запасной" /></label>
          <label className="field"><span>Ресурс Anthropic (необязательно)</span>
            <input value={form.provider_resource_name} onChange={set('provider_resource_name')} spellCheck={false} /></label>
          <label className="field"><span>Ресурс OpenAI (необязательно)</span>
            <input value={form.openai_provider_resource_name} onChange={set('openai_provider_resource_name')} spellCheck={false} /></label>
        </div>
        <figure className="add-guide-figure">
          <AddGuide />
          <figcaption className="muted small">Откройте приложение в редакторе Retool, затем DevTools → Network и любой запрос к Retool</figcaption>
        </figure>
      </div>
      <p className="muted small">
        Cookie и xsrf сохраняются только в config.json рядом с relay. Аккаунт подключится после перезапуска.
        Ресурсы нужны, если у аккаунта другой адрес Retool: без них у него не будет маршрута к моделям
      </p>
      <div className="form-actions">
        <Button onClick={onCancel}>Отмена</Button>
        <Button tone="primary" type="submit" disabled={!ready || busy}>{busy ? 'Сохранение' : 'Добавить'}</Button>
      </div>
    </form>
  );
}

export default function Accounts({ apiKey, onUnauthorized }) {
  const toast = useToast();
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState({});
  const [adding, setAdding] = useState(false);

  async function load() {
    try {
      setData(await api(apiKey, 'accounts'));
      setError('');
    } catch (failure) {
      if (failure.status === 401) onUnauthorized(); else setError(failure.message);
    }
  }
  usePolling(load, 10000, [apiKey]);

  async function run(tag, label, request, success) {
    setBusy((b) => ({ ...b, [tag]: true }));
    try {
      const result = await request();
      await load();
      const message = success(result);
      if (message) toast(message);
    } catch (failure) {
      if (failure.status === 401) onUnauthorized();
      else toast({ tone: 'error', title: label, text: failure.message });
    } finally {
      setBusy((b) => ({ ...b, [tag]: false }));
    }
  }

  const accounts = data?.accounts || [];
  const summary = data?.summary;
  const enabledCount = accounts.filter((a) => a.enabled !== false && a.pending !== 'removed').length;

  const action = (account) => (kind) => run(account.account, kind === 'balance' ? 'Баланс не получен' : 'Сессия не продлена',
    () => api(apiKey, `accounts/${account.account}/${kind}`, { method: 'POST', body: {} }),
    (result) => (kind === 'balance'
      ? { tone: 'success', title: 'Баланс обновлён', text: `${account.label || account.account}: ${fmtFixed(result?.account?.available, 2)}` }
      : result?.ok ? { tone: 'success', title: 'Сессия продлена', text: account.label || account.account }
        : { tone: 'error', title: 'Сессия не продлена', text: result?.error || result?.account?.error || 'Retool отклонил продление' }));
  const patch = (account) => (body) => run(account.account, 'Не сохранено',
    () => api(apiKey, `accounts/${account.account}`, { method: 'PATCH', body }), () => null);
  const remove = (account) => () => {
    if (!window.confirm(`Удалить аккаунт ${account.label || account.account} из config.json? Его cookie будут стёрты`)) return;
    run(account.account, 'Не удалено', () => api(apiKey, `accounts/${account.account}`, { method: 'DELETE', body: {} }),
      (result) => (result?.pending === 'removed'
        ? { tone: 'warn', title: 'Аккаунт удалён', text: 'Исчезнет из пула после перезапуска relay' }
        : { tone: 'success', title: 'Аккаунт удалён', text: 'Он ещё не был подключён, перезапуск не нужен' }));
  };
  const add = (form) => run('_new', 'Не добавлено',
    () => api(apiKey, 'accounts', { method: 'POST',
      // Optional resource names are sent only when filled in.
      body: Object.fromEntries(Object.entries(form).map(([k, v]) => [k, v.trim()]).filter(([k, v]) => v || REQUIRED.includes(k))) }),
    () => { setAdding(false); return { tone: 'success', title: 'Аккаунт добавлен', text: 'Подключится после перезапуска relay' }; });

  return (
    <Page className="accounts">
      <header className="page-head">
        <div>
          <h1>Аккаунты</h1>
          <p className="page-meta">
            {summary ? `в пуле ${fmtInt(summary.total)} · включено ${fmtInt(summary.enabled ?? enabledCount)} · в работе ${fmtInt(summary.routable)}` : 'пул аккаунтов Retool'}
            {isNum(data?.limits?.min_account_balance) && ` · резерв ниже ${fmtInt(data.limits.min_account_balance)} кредитов`}
          </p>
        </div>
        <div className="head-actions">
          <Button icon={RefreshCw} onClick={load}>Обновить</Button>
          <Button icon={Plus} tone="primary" onClick={() => setAdding(true)} disabled={adding}>Добавить аккаунт</Button>
        </div>
      </header>
      {error && <div className="banner tone-neg" role="alert">{error}</div>}
      {data?.restart_required && (
        <div className="banner tone-warn">Есть изменения пула, которые вступят в силу после перезапуска relay</div>
      )}
      {adding && <AddForm busy={!!busy._new} onCancel={() => setAdding(false)} onSubmit={add} />}
      <section className="panel pool">
        <div className="pool-help">
          <PoolScene />
          <dl className="pool-rules">
            <div><dt>Порядок</dt><dd>Новый диалог получает включённый аккаунт с наименьшим приоритетом, при равенстве — менее загруженный. Начатый диалог остаётся на своём аккаунте</dd></div>
            <div><dt>Резерв</dt><dd>Аккаунт с балансом ниже {isNum(data?.limits?.min_account_balance) ? fmtInt(data.limits.min_account_balance) : 'порога'} кредитов отходит в резерв и получает запросы, только когда ниже порога все</dd></div>
            <div><dt>Выключен</dt><dd>Не получает запросов, не продлевается и не прогревает кэш. Его диалоги переезжают на другие аккаунты</dd></div>
          </dl>
        </div>
        {accounts.length > 0 && (
          <ul className="pool-list">
            {accounts.map((account) => (
              <AccountRow key={account.account} account={account} busy={!!busy[account.account]}
                lastEnabled={account.enabled !== false && enabledCount <= 1}
                onAction={action(account)} onPatch={patch(account)} onDelete={remove(account)} />
            ))}
          </ul>
        )}
        {data && !accounts.length && <Empty title="В пуле нет аккаунтов" text="Добавьте аккаунт Retool, чтобы relay мог отвечать" />}
      </section>
    </Page>
  );
}
