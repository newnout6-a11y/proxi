import { useState } from 'react';
import { RefreshCw, TriangleAlert } from 'lucide-react';
import { api } from '../api.js';
import { usePolling } from '../useJournal.js';
import { AnimatedNumber, Dot, Page } from '../motion.jsx';
import { CacheChart, RankedList, TurnTape } from '../charts.jsx';
import { Button, CopyButton, Tag } from '../ui.jsx';
import { ACCOUNT_STATE, STATUS_TONE, TURN_STATUS } from '../catalog.js';
import { buildTraces, phasesOf } from '../turns.js';
import {
  fmtAgo, fmtClock, fmtDay, fmtDuration, fmtFixed, fmtInt, fmtMs, fmtNum, fmtPercent, fmtTokens, fmtUptime,
  isNum, parseDay, plural,
} from '../format.js';

export const WINDOWS = [
  { id: 3600, label: '1 ч', long: 'последний час' },
  { id: 21600, label: '6 ч', long: 'последние 6 часов' },
  { id: 86400, label: '24 ч', long: 'последние сутки' },
];

const SESSION_MODE = { per_conversation: 'сессия на каждый диалог', shared: 'одна общая сессия' };

const pct = (share) => `${Math.max(0, Math.min(1, share)) * 100}%`;

function Segmented({ value, onChange, label }) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {WINDOWS.map((item) => (
        <button key={item.id} type="button" aria-pressed={value === item.id} onClick={() => onChange(item.id)}>
          {item.label}
        </button>
      ))}
    </div>
  );
}

/** The balance as an instrument: figure, month scale with a now marker, and a burn-rate forecast. */
function Balance({ balance, metrics, windowSec, overview }) {
  const available = balance?.available;
  const allocation = balance?.monthly_allocation;
  const used = isNum(balance?.used_month) ? balance.used_month
    : isNum(allocation) && isNum(available) ? allocation - available : null;
  const reset = parseDay(balance?.reset_date);
  const daysLeft = reset ? Math.max(0, (reset.getTime() - Date.now()) / 86400000) : null;
  const elapsed = daysLeft !== null ? Math.max(0, Math.min(1, 1 - daysLeft / 30)) : null;
  const usedShare = isNum(used) && allocation ? used / allocation : null;
  const credits = metrics?.totals?.credits;
  const perDay = isNum(credits) && credits > 0 ? (credits * 86400) / windowSec : null;
  const lasts = perDay && isNum(available) ? available / perDay : null;
  const window = WINDOWS.find((w) => w.id === windowSec)?.long || 'окно';

  let forecast;
  if (!metrics) forecast = <span>Прогноз появится после загрузки метрик</span>;
  else if (!perDay) forecast = <span>За {window} кредиты не тратились, прогноза нет</span>;
  else {
    forecast = (
      <span>
        При темпе <b className="num">{fmtFixed(perDay, 2)}</b> в сутки хватит примерно на <b className="num">{fmtDuration(lasts)}</b>
        {daysLeft !== null && lasts > daysLeft ? ', дольше чем до сброса' : ''}
      </span>
    );
  }

  return (
    <section className="panel instrument" aria-label="Баланс">
      <div className="instrument-head">
        <span className="instrument-label">Доступно кредитов</span>
        {balance?.stale && <Tag tone="warn">данные устарели</Tag>}
      </div>
      <p className="figure-xl">
        <AnimatedNumber value={available} format={(v) => fmtFixed(v, 2)} />
        {isNum(allocation) && <span className="figure-unit">из {fmtInt(allocation)}</span>}
      </p>
      <div className="scale" role="img"
        aria-label={`Израсходовано ${fmtPercent(usedShare)} лимита месяца, прошло ${fmtPercent(elapsed)} периода`}>
        {usedShare !== null && <span className="scale-used" style={{ width: pct(usedShare) }} />}
        {elapsed !== null && <span className="scale-now" style={{ left: pct(elapsed) }} title="Сегодня" />}
      </div>
      <div className="scale-legend">
        <span>израсходовано {fmtNum(used, 2)} · {fmtPercent(usedShare)}</span>
        <span>
          сброс {fmtDay(balance?.reset_date)}
          {daysLeft !== null && ` · через ${Math.ceil(daysLeft)} ${plural(Math.ceil(daysLeft), ['день', 'дня', 'дней'])}`}
        </span>
      </div>
      <p className="forecast">{forecast}</p>
      <div className="instrument-foot">
        <span>потрачено с запуска <b className="num">{fmtFixed(overview?.usage?.session_used, 2)}</b></span>
        <span>ходов с запуска <b className="num">{fmtInt(overview?.usage?.turns)}</b></span>
        <span>аптайм <b className="num">{fmtUptime(overview?.uptime_seconds)}</b></span>
      </div>
    </section>
  );
}

function Stat({ label, value, note, tone }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className="stat-value num">{value}</span>
      <span className={`stat-note${tone ? ` tone-${tone}` : ''}`}>{note}</span>
    </div>
  );
}

function Accounts({ accounts, summary, limits }) {
  if (!accounts?.length) return null;
  return (
    <section className="panel" aria-label="Аккаунты">
      <div className="panel-head">
        <h2>Аккаунты</h2>
        <span className="panel-meta">
          {fmtInt(summary?.routable ?? accounts.filter((a) => a.routable).length)} из {fmtInt(summary?.total ?? accounts.length)} в работе
          {isNum(limits?.min_account_balance) && ` · резерв ниже ${fmtInt(limits.min_account_balance)}`}
        </span>
      </div>
      <ul className="acc-list">
        {accounts.map((account) => {
          const state = ACCOUNT_STATE[account.state] || { label: account.state || '—', tone: 'muted' };
          const share = isNum(account.available) && account.monthly_allocation ? account.available / account.monthly_allocation : null;
          return (
            <li className="acc" key={account.account}>
              <span className="acc-name"><Dot tone={state.tone} />{account.account}</span>
              <span className="acc-state">
                {state.label}
                {account.reserve && <Tag tone="warn">резерв</Tag>}
                {account.stale && <Tag tone="warn">устарело</Tag>}
              </span>
              <span className="acc-money">
                <span className="acc-money-row">
                  <b className="num">{fmtFixed(account.available, 2)}</b>
                  <span className="muted num">из {fmtInt(account.monthly_allocation)}</span>
                </span>
                <span className="acc-bar">{share !== null && <span style={{ width: pct(share) }} />}</span>
              </span>
              <span className="acc-meta">
                {account.error ? <span className="neg-text" title={account.error}>{account.error}</span>
                  : `${fmtInt(account.turns)} ${plural(account.turns || 0, ['ход', 'хода', 'ходов'])} · ${fmtFixed(account.session_used, 2)}`}
                {account.auth_failures > 0 && ` · отказов входа ${account.auth_failures}`}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function RecentTurns({ traces, go }) {
  const list = traces.slice(0, 6);
  const max = Math.max(1, ...list.map((trace) => phasesOf(trace).total));
  return (
    <section className="panel" aria-label="Последние ходы">
      <div className="panel-head">
        <h2>Последние ходы</h2>
        <Button onClick={() => go('turns')}>Все ходы</Button>
      </div>
      {list.length ? (
        <ul className="recent">
          {list.map((trace) => (
            <li className="recent-row" key={trace.id}>
              <Tag tone={STATUS_TONE[trace.status] || 'muted'}>{TURN_STATUS[trace.status] || trace.status}</Tag>
              <span className="recent-model">
                <strong>{trace.model || '—'}</strong>
                <small>{[trace.effort && `effort ${trace.effort}`, fmtAgo(trace.started)].filter(Boolean).join(' · ')}</small>
              </span>
              <TurnTape trace={trace} max={max} />
              <span className="recent-dur">{fmtMs(phasesOf(trace).total)}</span>
            </li>
          ))}
        </ul>
      ) : <p className="muted small">Ходов пока нет</p>}
    </section>
  );
}

function Feed({ journal, go }) {
  const recent = journal.events.slice(-8).reverse();
  const live = journal.status === 'live';
  return (
    <section className="panel" aria-label="Журнал сейчас">
      <div className="panel-head">
        <h2><Dot tone={live ? 'pos' : 'warn'} live={live} /> Журнал</h2>
        <Button onClick={() => go('journal')}>Открыть</Button>
      </div>
      <ul className="feed">
        {recent.map((event) => (
          <li key={event.seq} className={`feed-row level-${event.level}`}>
            <Dot tone={{ success: 'pos', error: 'neg', warn: 'warn' }[event.level] || 'muted'} />
            <span className="feed-msg" title={event.message}>{event.message}</span>
            <time title={fmtClock(event.ts)}>{fmtAgo(event.ts)}</time>
          </li>
        ))}
        {!recent.length && <li className="muted small">Событий пока нет</li>}
      </ul>
    </section>
  );
}

function Connection({ overview }) {
  const rows = [['OpenAI base URL', overview?.connection?.openai_base_url], ['Anthropic base URL', overview?.connection?.anthropic_base_url]];
  return (
    <section className="panel connect" aria-label="Подключение IDE">
      <div className="panel-head"><h2>Подключение IDE</h2></div>
      {rows.map(([label, value]) => (
        <div className="connect-row" key={label}>
          <span>{label}</span>
          <code>{value || '—'}</code>
          {value && <CopyButton value={value} label={`Копировать ${label}`} />}
        </div>
      ))}
      <div className="connect-row">
        <span>Модель по умолчанию</span>
        <code>{overview?.models?.default || '—'}</code>
      </div>
      <p className="muted small">Ключ тот же relay key: заголовок <code>Authorization: Bearer</code> или <code>x-api-key</code></p>
      <div className="session-state">
        <Dot tone={overview?.session?.error ? 'neg' : 'pos'} />
        <span>Сессия Retool: {overview?.session?.error ? overview.session.error : `продлена ${fmtAgo(overview?.session?.last_success_at)}`}</span>
      </div>
    </section>
  );
}

export default function Overview({ apiKey, journal, overview, overviewError, refreshOverview, onUnauthorized, go }) {
  const [windowSec, setWindowSec] = useState(3600);
  // Keyed by window: a late answer for the previous window can never overwrite the current one.
  const [metrics, setMetrics] = useState({});
  const [error, setError] = useState('');

  async function loadMetrics() {
    const requested = windowSec;
    try {
      const data = await api(apiKey, `metrics?window=${requested}`);
      setMetrics((previous) => ({ ...previous, [requested]: data }));
      setError('');
    } catch (failure) {
      if (failure.status === 401) onUnauthorized();
      else setError(failure.message);
    }
  }
  usePolling(loadMetrics, 8000, [apiKey, windowSec]);

  const current = metrics[windowSec] || null;
  const totals = current?.totals || {};
  const buckets = current?.buckets || [];
  const models = Object.entries(current?.models || {}).map(([label, value]) => ({ label, value })).sort((a, b) => b.value - a.value);
  const ok = overview?.health?.ok;
  const traces = buildTraces(journal.events);
  const problem = error || overviewError || overview?.health?.error;
  const label = WINDOWS.find((w) => w.id === windowSec).long;

  return (
    <Page className="overview">
      <header className="page-head">
        <div>
          <h1>Обзор</h1>
          <p className="page-meta">
            {label} · {SESSION_MODE[overview?.sessions?.mode] || 'режим сессий неизвестен'} · активных запросов {fmtInt(overview?.sessions?.active)} · диалогов {fmtInt(overview?.sessions?.cached)}
          </p>
        </div>
        <div className="head-actions">
          {overview && (
            <span className="status"><Dot tone={ok ? 'pos' : 'neg'} live={ok} />{ok ? 'Retool отвечает' : 'Retool недоступен'}</span>
          )}
          <Segmented value={windowSec} onChange={setWindowSec} label="Окно метрик" />
          <Button icon={RefreshCw} onClick={() => { refreshOverview(); loadMetrics(); }} aria-label="Обновить обзор">Обновить</Button>
        </div>
      </header>

      {problem && <div className="banner tone-neg" role="alert"><TriangleAlert size={16} strokeWidth={1.5} /> {problem}</div>}

      <div className="ov-top">
        <Balance balance={overview?.balance} metrics={current} windowSec={windowSec} overview={overview} />
        <section className="panel stats" aria-label="Показатели за окно">
          <Stat label="Запросы" value={fmtInt(totals.requests)}
            note={totals.errors ? `${fmtInt(totals.errors)} ${plural(totals.errors, ['ошибка', 'ошибки', 'ошибок'])}` : 'без ошибок'}
            tone={totals.errors ? 'neg' : undefined} />
          <Stat label="Попадание в кэш" value={fmtPercent(totals.cache_hit_ratio)}
            note={`чтение ${fmtTokens(totals.cache_read)} · запись ${fmtTokens(totals.cache_write)} · без кэша ${fmtTokens(totals.input_tokens)}`} />
          <Stat label="Первый токен, p50" value={fmtMs(totals.ttft_p50)} note={`p95 ${fmtMs(totals.ttft_p95)}`} />
          <Stat label="Ответ целиком, p50" value={fmtMs(totals.latency_p50)} note={`p95 ${fmtMs(totals.latency_p95)}`} />
        </section>
      </div>

      <div className="ov-main">
        <section className="panel chart-panel" aria-label="Кэш промпта и кредиты">
          <div className="panel-head">
            <h2>Кэш промпта и кредиты</h2>
            <ul className="legend">
              <li><i className="lg-read" />чтение над осью</li>
              <li><i className="lg-write" />запись под осью</li>
              <li><i className="lg-credits" />кредиты <b className="num">{fmtFixed(totals.credits, 2)}</b></li>
            </ul>
          </div>
          <CacheChart buckets={buckets} windowSec={windowSec} />
        </section>
        <div className="ov-side">
          <section className="panel" aria-label="Модели">
            <div className="panel-head">
              <h2>Модели</h2>
              <span className="panel-meta">ходов {fmtInt(totals.turns)} · инструментов {fmtInt(totals.tools)}</span>
            </div>
            <RankedList items={models} empty="За окно ходов не было" />
          </section>
          <Connection overview={overview} />
        </div>
      </div>

      <Accounts accounts={overview?.accounts} summary={overview?.accounts_summary} limits={overview?.limits} />

      <div className="ov-bottom">
        <RecentTurns traces={traces} go={go} />
        <Feed journal={journal} go={go} />
      </div>
    </Page>
  );
}
