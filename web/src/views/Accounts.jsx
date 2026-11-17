import { useState } from 'react';
import { Plus, RefreshCw, RotateCw, Trash, Wallet, X } from 'lucide-react';
import { api } from '../api.js';
import { usePolling } from '../useJournal.js';
import { Dot, Page } from '../motion.jsx';
import { Button, Empty, Tag, Toggle, useToast } from '../ui.jsx';
import { ACCOUNT_STATE } from '../catalog.js';
import { fmtAgo, fmtDay, fmtFixed, fmtInt, fmtNum, fmtShortTime, isNum, plural } from '../format.js';
import { describeResult, tokenExpiry, tokenLines, WARN_DAYS } from '../minara.js';
import { PoolScene } from '../stage/PoolScene.jsx';
import { AddGuide, GuideNum } from '../stage/AddGuide.jsx';

const pct = (share) => `${Math.max(0, Math.min(1, share)) * 100}%`;
const TOKEN_TONE = { expired: 'neg-text', expiring: 'warn-text' };

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
  const minara = account.upstream === 'minara';
  // A Bearer token has no renewal: the moment it expires is what the operator watches (the relay warns WARN_DAYS before)
  const token = minara ? tokenExpiry(account.expires_at) : null;
  const expired = token?.state === 'expired';
  const state = account.enabled === false ? { label: 'выключен', tone: 'muted' }
    : expired && account.state === 'auth_dead' ? { label: 'токен истёк', tone: 'neg' }
      : ACCOUNT_STATE[account.state] || { label: account.state || '—', tone: 'muted' };
  const share = isNum(account.available) && account.monthly_allocation ? account.available / account.monthly_allocation : null;
  const name = account.label || account.account;
  const pending = account.pending === 'added' ? 'подключится после перезапуска'
    : account.pending === 'removed' ? 'удалён, исчезнет после перезапуска' : null;
  // The token line says why an expired account is out; the code itself would only repeat it
  const error = expired && account.error === 'token_expired' ? null : account.error;
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
        {account.enabled !== false && isNum(account.rate_limited_until) && (
          <Tag tone="warn" title="Retool ответил: часовой лимит managed-ключей исчерпан. Ходы идут на другие аккаунты">
            вернётся в {fmtShortTime(account.rate_limited_until)}
          </Tag>
        )}
        {account.enabled !== false && token?.state === 'expiring' && (
          <Tag tone="warn" title={`Осталось меньше ${WARN_DAYS} дней: замените токен в config.json, иначе аккаунт выйдет из пула`}>
            токен истекает
          </Tag>
        )}
        {account.stale && <Tag tone="warn">устарело</Tag>}
        {pending && <Tag tone="accent">{pending}</Tag>}
      </div>
      <div className="pool-money">
        <span className={`pool-money-row${minara ? ' is-wrapping' : ''}`}>
          <b className="num">{fmtFixed(account.available, 2)}</b>
          <span className="muted num">{minara
            ? `${isNum(account.monthly_allocation) ? `из ${fmtInt(account.monthly_allocation)} ` : ''}кредитов Minara${isNum(account.usd) ? ` · ${fmtFixed(account.usd, 2)} USD` : ''}`
            : `из ${fmtInt(account.monthly_allocation)}`}</span>
        </span>
        <span className="acc-bar">{share !== null && <span style={{ width: pct(share) }} />}</span>
        <span className="pool-meta">
          {minara
            ? account.turns ? `${fmtInt(account.turns)} ${plural(account.turns, ['ход', 'хода', 'ходов'])} · ${fmtInt(account.attempts)} ${plural(account.attempts || 0, ['попытка', 'попытки', 'попыток'])} · цена ${isNum(account.provider_cost) ? `${fmtNum(account.provider_cost)} ${account.provider_cost_currency || '(валюта неизвестна)'}` : 'неизвестна'}`
              : 'ходов ещё не было'
            : `сброс ${fmtDay(account.reset_date)} · с запуска ${fmtFixed(account.session_used, 2)} за ${fmtInt(account.turns)} ${plural(account.turns || 0, ['ход', 'хода', 'ходов'])}`}
        </span>
      </div>
      <div className="pool-health">
        <span>баланс: {account.pending === 'added' ? 'после перезапуска' : account.balance_supported === false ? 'провайдер не сообщает' : account.last_balance_at ? fmtAgo(account.last_balance_at) : 'ещё не проверялся'}</span>
        {minara ? <span className={TOKEN_TONE[token.state]}>{token.text}</span>
          : <span>сессия: {account.last_renewed_at ? fmtAgo(account.last_renewed_at) : 'ещё не продлевалась'}</span>}
        {account.auth_failures > 0 && <span className="neg-text">отказов входа подряд: {account.auth_failures}</span>}
        {error && <span className="neg-text" title={error}>{error}</span>}
      </div>
      <div className="pool-controls">
        <Priority value={account.priority} disabled={!!account.pending || busy} onSave={(priority) => onPatch({ priority })} />
        <Toggle id={`enabled-${account.account}`} checked={account.enabled !== false}
          label={`Аккаунт ${name} в маршрутизации`}
          disabled={busy || account.pending === 'removed' || (account.enabled !== false && lastEnabled)}
          title={account.pending === 'removed' ? 'Аккаунт удалён и исчезнет после перезапуска'
            : account.enabled !== false && lastEnabled ? 'Это последний включённый аккаунт: без него relay не сможет отвечать' : undefined}
          onChange={(enabled) => onPatch({ enabled })} />
      </div>
      <div className="pool-actions">
        <Button icon={Wallet} disabled={busy || !!account.pending || account.balance_supported === false || expired}
          title={expired ? 'Токен истёк: баланс не запрашивается, нужен новый токен' : undefined} onClick={() => onAction('balance')}>Баланс</Button>
        {/* A Bearer token is renewed by adding a newer token of its user, never by the relay: no button that cannot work */}
        {!(minara && account.renewal_supported === false) && (
          <Button icon={RotateCw} disabled={busy || !!account.pending || account.renewal_supported === false} onClick={() => onAction('renew')}>Продлить</Button>
        )}
        <button type="button" className="icon-button tone-danger" aria-label={`Удалить аккаунт ${name}`} title="Удалить из config.json"
          disabled={busy || account.pending === 'removed'} onClick={onDelete}>
          <Trash size={16} strokeWidth={1.5} />
        </button>
      </div>
    </li>
  );
}

const EMPTY_FORM = { upstream: 'retool', label: '', base: '', page_uuid: '', cookie: '', xsrf: '', provider_resource_name: '', openai_provider_resource_name: '', bearer_tokens: '', bearer_token_env: '', minara_models: '' };
const REQUIRED = ['base', 'page_uuid', 'cookie', 'xsrf'];

/** What a bulk add skipped, line by line; the added accounts are in the list below. */
function TokenReport({ report }) {
  const lineOf = (position) => report.lines[position - 1] ?? position;
  const skipped = report.results.filter((result) => result.status !== 'added');
  const renewed = report.renewed || 0;
  return (
    <section className="token-report" aria-label="Результат по токенам" role="status">
      <p className="token-report-head">
        Добавлено {fmtInt(report.added)}{renewed ? `, обновлено ${fmtInt(renewed)}` : ''} из {fmtInt(report.results.length)}{report.added || renewed ? ', подключатся после перезапуска' : ''}
      </p>
      {skipped.length > 0 && (
        <ol className="token-report-list">
          {skipped.map((result) => {
            const view = describeResult(result, lineOf);
            return (
              <li key={result.token}>
                <span className="token-report-line">строка {lineOf(result.token)}</span>
                <Tag tone={view.tone}>{view.tag}</Tag>
                <span className="token-report-text">{view.text}</span>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

function AddForm({ onCancel, onSubmit, busy, report }) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [modelError, setModelError] = useState('');
  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));
  const minara = form.upstream === 'minara';
  const tokens = minara ? tokenLines(form.bearer_tokens) : [];
  const env = form.bearer_token_env.trim();
  // Tokens in the field or one environment variable: never both, so it is always clear which accounts are added
  const ready = minara ? tokens.length > 0 || !!env
    : form.base.trim() && form.page_uuid.trim() && form.cookie.trim() && form.xsrf.trim();
  return (
    <form className="panel add-form" onSubmit={(e) => {
      e.preventDefault();
      if (!ready) return;
      let models;
      if (minara && form.minara_models.trim()) {
        try { models = JSON.parse(form.minara_models); if (!Array.isArray(models) || !models.length) throw new Error(); }
        catch { setModelError('Укажите JSON-массив моделей или оставьте поле пустым'); return; }
      }
      setModelError(''); onSubmit(form, { tokens, models });
    }}>
      <div className="panel-head">
        <h2>Новый аккаунт</h2>
        <button type="button" className="icon-button ghost" aria-label="Закрыть форму" onClick={onCancel}><X size={16} strokeWidth={1.5} /></button>
      </div>
      <div className={`add-layout${minara ? ' add-layout-bearer' : ''}`}>
        <div className="form-grid">
          <label className="field field-wide"><span>Провайдер</span>
            <select aria-label="Провайдер" value={form.upstream} onChange={(e) => setForm((f) => ({ ...f, upstream: e.target.value, base: e.target.value === 'minara' ? 'https://api.minara.ai/v1' : '' }))}>
              <option value="retool">Retool · Cookie</option><option value="minara">Minara · Bearer</option>
            </select></label>
          {minara ? <>
            <label className="field field-wide"><span>Bearer-токены, по одному на строку</span>
              <textarea className="token-lines" value={form.bearer_tokens} onChange={set('bearer_tokens')} disabled={!!env}
                rows={5} spellCheck={false} autoComplete="off" aria-describedby="minara-tokens-hint" />
              <span id="minara-tokens-hint" className="hint">
                {tokens.length ? `Токенов: ${fmtInt(tokens.length)}. ` : ''}Строка — один аккаунт. Токен из пула пропускается, новый токен того же пользователя обновляет его аккаунт
              </span></label>
            <label className="field"><span>Или переменная окружения с токеном</span>
              <input value={form.bearer_token_env} onChange={set('bearer_token_env')} disabled={tokens.length > 0} placeholder="MINARA_API_TOKEN" spellCheck={false} autoComplete="off" /></label>
            <label className="field"><span>Название</span>
              <input value={form.label} onChange={set('label')} maxLength={40} placeholder="Minara N по порядку" aria-describedby="minara-label-hint" />
              <span id="minara-label-hint" className="hint">Для нескольких токенов — начало названий: Команда 1, Команда 2</span></label>
            <label className="field field-wide"><span>Модели (необязательно)</span>
              <textarea value={form.minara_models} onChange={(e) => { set('minara_models')(e); setModelError(''); }} aria-invalid={!!modelError}
                aria-describedby={modelError ? 'minara-model-error' : 'minara-model-hint'} rows={2} placeholder={'["minara/claude-opus-5-5", "minara/gpt-6.1-sol"]'} spellCheck={false} />
              {modelError ? <span id="minara-model-error" className="neg-text" role="alert">{modelError}</span>
                : <span id="minara-model-hint" className="hint">Пусто — все опубликованные модели minara/…; JSON-массив — только перечисленные</span>}</label>
            <label className="field field-wide"><span>Адрес API</span><input value={form.base} onChange={set('base')} spellCheck={false} /></label>
          </> : <>
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
          </>}
        </div>
        {!minara && <figure className="add-guide-figure">
          <AddGuide />
          <figcaption className="muted small">Откройте приложение в редакторе Retool, затем DevTools → Network и любой запрос к Retool</figcaption>
        </figure>}
      </div>
      {minara && report && <TokenReport report={report} />}
      <p className="muted small">
        {minara ? `Токен — JWT из веб-приложения Minara, живёт 30 дней; за ${WARN_DAYS} дня до конца relay предупредит в журнале. Токены сохраняются в config.json, у переменной окружения — только её имя. 220 кредитов — 1 USD.`
          : 'Cookie и xsrf сохраняются только в config.json рядом с relay. Ресурсы нужны, если у аккаунта другой адрес Retool.'} {minara ? 'Изменения подключатся' : 'Аккаунт подключится'} после перезапуска.
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
  const [report, setReport] = useState(null);

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
  // Only accounts that serve now count: an added one waits for a restart, a removed one is leaving
  const enabledCount = accounts.filter((a) => a.enabled !== false && !a.pending).length;

  const action = (account) => (kind) => run(account.account, kind === 'balance' ? 'Баланс не получен' : 'Сессия не продлена',
    () => api(apiKey, `accounts/${account.account}/${kind}`, { method: 'POST', body: {} }),
    (result) => (kind === 'balance'
      ? { tone: 'success', title: 'Баланс обновлён', text: `${account.label || account.account}: ${fmtFixed(result?.account?.available, 2)}` }
      : result?.ok ? { tone: 'success', title: 'Сессия продлена', text: account.label || account.account }
        : { tone: 'error', title: 'Сессия не продлена', text: result?.error || result?.account?.error || 'Retool отклонил продление' }));
  const patch = (account) => (body) => run(account.account, 'Не сохранено',
    () => api(apiKey, `accounts/${account.account}`, { method: 'PATCH', body }), () => null);
  const remove = (account) => () => {
    if (!window.confirm(`Удалить аккаунт ${account.label || account.account} из config.json? Его учётные данные будут стёрты`)) return;
    run(account.account, 'Не удалено', () => api(apiKey, `accounts/${account.account}`, { method: 'DELETE', body: {} }),
      (result) => (result?.pending === 'removed'
        ? { tone: 'warn', title: 'Аккаунт удалён', text: 'Исчезнет из пула после перезапуска relay' }
        : { tone: 'success', title: 'Аккаунт удалён', text: 'Он ещё не был подключён, перезапуск не нужен' }));
  };
  const add = (form, { tokens = [], models } = {}) => {
    const minara = form.upstream === 'minara';
    // Several tokens (or one) go as one request: each is checked and reported on its own, the token itself never comes back
    const bulk = minara && tokens.length > 0;
    const base = form.base.trim() || 'https://api.minara.ai/v1';
    const label = form.label.trim();
    const body = !minara
      // Optional resource names are sent only when filled in.
      ? Object.fromEntries(Object.entries(form).filter(([k]) => !['bearer_tokens', 'bearer_token_env', 'minara_models', 'upstream'].includes(k)).map(([k, v]) => [k, v.trim()]).filter(([k, v]) => v || REQUIRED.includes(k)))
      : bulk ? { upstream: 'minara', base, bearer_tokens: tokens.map((item) => item.token), ...(label ? { label } : {}), ...(models ? { minara_models: models } : {}) }
        : { upstream: 'minara', base, label, bearer_token_env: form.bearer_token_env.trim(), ...(models ? { minara_models: models } : {}) };
    setReport(null);
    return run('_new', 'Не добавлено', () => api(apiKey, 'accounts', { method: 'POST', body }), (result) => {
      if (!bulk) {
        setAdding(false);
        return { tone: 'success', title: 'Аккаунт добавлен', text: 'Подключится после перезапуска relay' };
      }
      if (!result?.skipped) {
        setAdding(false);
        const added = result?.added || 0;
        const renewed = result?.renewed || 0;
        const accounts = (n) => `${fmtInt(n)} ${plural(n, ['аккаунт', 'аккаунта', 'аккаунтов'])}`;
        return { tone: 'success', title: added
          ? `Добавлено: ${accounts(added)} Minara${renewed ? `, токен обновлён у ${fmtInt(renewed)}` : ''}`
          : `Токен обновлён у ${accounts(renewed)} Minara`,
        text: 'Подключатся после перезапуска relay' };
      }
      // Something was skipped: the form stays open with what happened to each line
      setReport({ results: result.results || [], added: result.added || 0, renewed: result.renewed || 0,
        lines: tokens.map((item) => item.line) });
      return null;
    });
  };
  const close = () => { setAdding(false); setReport(null); };

  return (
    <Page className="accounts">
      <header className="page-head">
        <div>
          <h1>Аккаунты</h1>
          <p className="page-meta">
            {summary ? `в пуле ${fmtInt(summary.total)} · включено ${fmtInt(summary.enabled ?? enabledCount)} · в работе ${fmtInt(summary.routable)}` : 'пул аккаунтов'}
            {/* Retool's reserve threshold; a Minara account works until Minara refuses it (minara_min_credits) */}
            {isNum(data?.limits?.min_account_balance) && accounts.some((a) => a.upstream !== 'minara')
              && ` · резерв ниже ${fmtNum(data.limits.min_account_balance)} кредитов Retool`}
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
      {adding && <AddForm busy={!!busy._new} report={report} onCancel={close} onSubmit={add} />}
      <section className="panel pool">
        <div className="pool-help">
          <figure className="pool-demo"><figcaption>Как работает пул <span>пример маршрутизации</span></figcaption><PoolScene /></figure>
          <dl className="pool-rules">
            <div><dt>Порядок</dt><dd>Новый диалог получает включённый аккаунт с наименьшим приоритетом, при равенстве — менее загруженный. Начатый диалог остаётся на своём аккаунте</dd></div>
            <div><dt>Резерв</dt><dd>Аккаунт с балансом ниже {isNum(data?.limits?.min_account_balance) ? fmtNum(data.limits.min_account_balance) : 'порога'} кредитов отходит в резерв и получает запросы, только когда ниже порога все</dd></div>
            <div><dt>Выключен</dt><dd>Не получает запросов, не продлевается и не прогревает кэш. Его диалоги переезжают на другие аккаунты</dd></div>
          </dl>
        </div>
        {accounts.length > 0 && (
          <ul className="pool-list">
            {accounts.map((account) => (
              <AccountRow key={account.account} account={account} busy={!!busy[account.account]}
                lastEnabled={!account.pending && account.enabled !== false && enabledCount <= 1}
                onAction={action(account)} onPatch={patch(account)} onDelete={remove(account)} />
            ))}
          </ul>
        )}
        {data && !accounts.length && <Empty title="В пуле нет аккаунтов" text="Добавьте аккаунт Retool, чтобы relay мог отвечать" />}
      </section>
    </Page>
  );
}
