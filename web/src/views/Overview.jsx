import { useState } from 'react';
import { RefreshCw, TriangleAlert } from 'lucide-react';
import { api } from '../api.js';
import { usePolling } from '../useJournal.js';
import { AnimatedNumber, Dot, Page } from '../motion.jsx';
import { CacheChart, RankedList, SpendChart, bucketLabel } from '../charts.jsx';
import { MIN_FORECAST_SECONDS, burnRate, coverageOf, fmtSince, fmtSpan } from '../coverage.js';
import { Button, Tag } from '../ui.jsx';
import {
  fmtDay, fmtDuration, fmtFixed, fmtInt, fmtMs, fmtPercent, fmtSpeed, fmtTokens, fmtUptime, isNum, parseDay, plural,
} from '../format.js';

export const WINDOWS = [
  { id: 3600, label: '1 ч', long: 'последний час' },
  { id: 21600, label: '6 ч', long: 'последние 6 часов' },
  { id: 86400, label: '24 ч', long: 'последние сутки' },
];

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

/**
 * The pool balance sums only the accounts whose balance is known, and the overview says how many were left out
 * (`partial`, `unknown_accounts`, `accounts_total`; the startup banner says the same): a lower total must not pass for
 * the whole pool. `unknown` tells missing balances from a missing monthly limit, which leaves the total whole.
 */
function partialNote(balance, summary) {
  if (balance?.partial !== true) return null;
  const left = isNum(balance.unknown_accounts) ? balance.unknown_accounts : 0;
  const total = isNum(balance.accounts_total) ? balance.accounts_total : summary?.total;
  if (left > 0) {
    return {
      unknown: true,
      text: isNum(total) ? `частично: ${fmtInt(left)} из ${fmtInt(total)} неизвестны` : 'частично: баланс известен не у всех',
      title: 'Баланс известен не у всех аккаунтов: сумма, лимит месяца и прогноз считаются только по аккаунтам с известным балансом',
    };
  }
  return {
    unknown: false,
    text: 'частично: лимит месяца известен не у всех',
    title: 'У части аккаунтов неизвестен лимит месяца: «из …» и доля израсходованного неполны',
  };
}

/** The pool balance as an instrument: figure, month scale with a today marker, and a burn-rate forecast. */
function Balance({ overview, metrics, windowSec, go }) {
  const balance = overview?.balance;
  const summary = overview?.accounts_summary;
  const partial = partialNote(balance, summary);
  const available = balance?.available;
  const allocation = balance?.monthly_allocation;
  const used = isNum(balance?.used_month) ? balance.used_month
    : isNum(allocation) && isNum(available) ? allocation - available : null;
  const reset = parseDay(balance?.reset_date);
  const daysLeft = reset ? Math.max(0, (reset.getTime() - Date.now()) / 86400000) : null;
  const elapsed = daysLeft !== null ? Math.max(0, Math.min(1, 1 - daysLeft / 30)) : null;
  const usedShare = isNum(used) && allocation ? used / allocation : null;
  const { seconds, short, perDay } = burnRate(metrics, windowSec);
  const lasts = perDay && isNum(available) ? available / perDay : null;
  const window = WINDOWS.find((w) => w.id === windowSec)?.long || 'окно';
  const basis = seconds !== null && seconds < windowSec ? fmtSpan(seconds) : window;

  let forecast;
  if (!metrics) forecast = 'Прогноз появится после загрузки метрик';
  else if (metrics.totals?.credits_complete === false || balance?.unreported_usage) forecast = 'Прогноз недоступен: часть стоимости не сообщена';
  else if (seconds === null) forecast = 'Прогноз недоступен: неизвестно, какую часть окна покрывает журнал';
  else if (short) forecast = `Прогноз недоступен: журнал покрывает меньше ${MIN_FORECAST_SECONDS / 60} минут`;
  else if (!perDay) forecast = `За ${basis} кредиты не тратились, прогноза нет`;
  else {
    forecast = (
      <>
        При темпе за {basis} (<b className="num">{fmtFixed(perDay, 2)}</b> в сутки){partial?.unknown ? ' по известным балансам' : ''} хватит примерно на <b className="num">{fmtDuration(lasts)}</b>
        {daysLeft !== null && lasts > daysLeft ? ', дольше чем до сброса' : ''}
      </>
    );
  }

  return (
    <section className="panel instrument" aria-label="Баланс пула">
      <div className="instrument-head">
        <span className="instrument-label">
          Доступно в пуле
          {summary && ` · ${fmtInt(summary.total)} ${plural(summary.total, ['аккаунт', 'аккаунта', 'аккаунтов'])}`}
        </span>
        {balance?.unreported_usage ? <Tag tone="warn">есть неучтённый расход</Tag> : balance?.stale && <Tag tone="warn">данные устарели</Tag>}
      </div>
      <p className="figure-xl">
        <AnimatedNumber value={available} format={(v) => fmtFixed(v, 2)} />
        {isNum(allocation) && <span className="figure-unit">из {fmtInt(allocation)}</span>}
        {partial && <Tag tone="warn" title={partial.title}>{partial.text}</Tag>}
      </p>
      <div className="scale" role="img"
        aria-label={`Израсходовано ${fmtPercent(usedShare)} лимита месяца, прошло ${fmtPercent(elapsed)} периода`}>
        {usedShare !== null && <span className="scale-used" style={{ width: pct(usedShare) }} />}
        {elapsed !== null && <span className="scale-now" style={{ left: pct(elapsed) }} title="Сегодня" />}
      </div>
      <div className="scale-legend">
        <span>израсходовано {fmtFixed(used, 2)} · {fmtPercent(usedShare)}</span>
        <span>
          сброс {fmtDay(balance?.reset_date)}
          {daysLeft !== null && ` · через ${Math.ceil(daysLeft)} ${plural(Math.ceil(daysLeft), ['день', 'дня', 'дней'])}`}
        </span>
      </div>
      <p className="forecast">{forecast}</p>
      <div className="instrument-foot">
        {summary && (
          <button type="button" className="link-button" onClick={() => go('accounts')}>
            в работе {fmtInt(summary.routable)} из {fmtInt(summary.total)}
            {summary.reserve > 0 && ` · резерв ${fmtInt(summary.reserve)}`}
          </button>
        )}
        <span>с запуска relay <b className="num">{fmtFixed(overview?.usage?.session_used, 2)}</b></span>
        <span>аптайм <b className="num">{fmtUptime(overview?.uptime_seconds)}</b></span>
      </div>
    </section>
  );
}

/**
 * The cache facts of the window. The cache refresh (`keepwarm_*`) is a part of the window's credits, so it is named
 * here and never added to them; a ping without a price makes its share a lower bound.
 */
function CacheFacts({ totals }) {
  const count = (value) => (isNum(value) && value > 0 ? value : 0);
  const warm = count(totals.keepwarm_credits);
  const pings = count(totals.keepwarm_pings);
  const unpriced = count(totals.keepwarm_unknown);
  return (
    <section className="panel cache-facts" aria-label="Попадание в кэш">
      <div className="panel-head"><h2>Попадание в кэш</h2><span className="figure">{fmtPercent(totals.cache_hit_ratio)}</span></div>
      <dl className="facts">
        <div><dt>прочитано из кэша</dt><dd className="num">{fmtTokens(totals.cache_read)}</dd></div>
        <div><dt>записано в кэш</dt><dd className="num">{fmtTokens(totals.cache_write)}</dd></div>
        <div><dt>без кэша</dt><dd className="num">{fmtTokens(totals.input_tokens)}</dd></div>
        {(warm > 0 || pings > 0) && (
          <div>
            <dt>прогрев кэша, кредиты</dt>
            <dd className="num" title={`Входит в расход за окно. Обращений для прогрева: ${fmtInt(pings)}${unpriced ? `, без цены: ${fmtInt(unpriced)}` : ''}`}>
              {unpriced ? '≥ ' : ''}{fmtFixed(warm, 2)}
            </dd>
          </div>
        )}
      </dl>
      <p className="muted small">Всплеск записи — рост объёма записанного кэша. Причину проверяйте по событиям конкретного хода</p>
    </section>
  );
}

/**
 * What the window's spend figure says besides its value, kept to one short line: a request without a valid price is a
 * gap (`credits_unknown`, cache refresh pings included) and a turn still waiting for its price is not one
 * (`credits_in_progress`, the figure will still grow). The cache refresh share is shown with the cache facts.
 */
function spendNote(totals, busy, span) {
  const count = (value) => (isNum(value) && value > 0 ? value : 0);
  const unknown = count(totals.credits_unknown);
  const waiting = count(totals.credits_in_progress);
  return [
    totals.credits_complete === false
      ? `часть стоимости не сообщена${unknown ? ` (без цены: ${fmtInt(unknown)})` : ''}`
      : busy ? `в среднем ${fmtFixed(totals.credits / busy, 2)} за активные ${span}` : 'расхода не было',
    waiting > 0 && `ждём цену: ${fmtInt(waiting)} ${plural(waiting, ['ход', 'хода', 'ходов'])}`,
  ].filter(Boolean).join(' · ');
}

function Stat({ label, value, note, tone, title }) {
  return (
    <div className="stat" title={title}>
      <span className="stat-label">{label}</span>
      <span className="stat-value num">{value}</span>
      <span className={`stat-note${tone ? ` tone-${tone}` : ''}`}>{note}</span>
    </div>
  );
}

export default function Overview({ apiKey, overview, overviewError, refreshOverview, onUnauthorized, go }) {
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
  const speedSamples = Number.isInteger(totals.speed_samples) && totals.speed_samples > 0 ? totals.speed_samples : 0;
  const buckets = current?.buckets || [];
  const models = Object.entries(current?.models || {}).map(([label, value]) => ({ label, value })).sort((a, b) => b.value - a.value);
  const ok = overview?.health?.ok;
  const problem = error || overviewError || overview?.health?.error;
  const label = WINDOWS.find((w) => w.id === windowSec).long;
  const span = bucketLabel(windowSec, buckets.length);
  const busy = buckets.filter((b) => (b.credits || 0) > 0).length;
  const unsure = buckets.some((b) => b.credits_complete === false);
  const coverage = coverageOf(current, windowSec);

  return (
    <Page className="overview">
      <header className="page-head">
        <div>
          <h1>Сводка</h1>
          <p className="page-meta">{label} · сколько тратится, на что и на сколько хватит</p>
        </div>
        <div className="head-actions">
          {overview && (
            <span className="status"><Dot tone={ok ? 'pos' : 'warn'} live={ok} />{ok ? 'Retool отвечает' : 'Состояние требует проверки'}</span>
          )}
          <Segmented value={windowSec} onChange={setWindowSec} label="Окно метрик" />
          <Button icon={RefreshCw} onClick={() => { refreshOverview(); loadMetrics(); }} aria-label="Обновить сводку">Обновить</Button>
        </div>
      </header>

      {problem && <div className="banner tone-neg" role="alert"><TriangleAlert size={16} strokeWidth={1.5} /> {problem}</div>}

      {coverage && (
        <div className="banner tone-warn">
          <TriangleAlert size={16} strokeWidth={1.5} aria-hidden="true" />
          <span>
            Журнал покрывает окно не полностью
            {coverage.seconds !== null && <>: данные за <b className="num">{fmtSpan(coverage.seconds)}</b> из окна {fmtSpan(windowSec)}</>}
            {coverage.from !== null && ` (с ${fmtSince(coverage.from)})`}.
            Суммы за окно неполные
          </span>
        </div>
      )}

      <div className="ov-top">
        <Balance overview={overview} metrics={current} windowSec={windowSec} go={go} />
        <section className="panel stats" aria-label="Показатели за окно">
          <Stat label="Потрачено за окно" value={totals.credits_complete === false ? totals.credits > 0 ? `≥ ${fmtFixed(totals.credits, 2)}` : '—' : fmtFixed(totals.credits, 2)}
            note={spendNote(totals, busy, span)} />
          <Stat label="Запросы" value={fmtInt(totals.requests)}
            note={totals.errors ? `${fmtInt(totals.errors)} ${plural(totals.errors, ['ошибка', 'ошибки', 'ошибок'])}` : 'без ошибок'}
            tone={totals.errors ? 'neg' : undefined} />
          <Stat label="Первый токен, p50" value={fmtMs(totals.ttft_p50)} note={`p95 ${fmtMs(totals.ttft_p95)}`} />
          <Stat label="Ответ целиком, p50" value={fmtMs(totals.latency_p50)} note={`p95 ${fmtMs(totals.latency_p95)}`} />
          <Stat label="Средняя скорость" value={fmtSpeed(speedSamples > 0 ? totals.tokens_per_second : null)}
            note={speedSamples > 0 ? `выход / всё время · ${fmtInt(speedSamples)} завершённых` : 'нет полных данных'}
            title="Сумма выходных токенов / сумма полного времени обработки успешно завершённых запросов с полными данными за выбранное окно. До завершения runner, включая очередь, мышление, повторы и смену аккаунта; без отправки последних байтов клиенту. Ошибки, отмена и обрезка лимитом исключены. Это не скорость декодирования." />
        </section>
      </div>

      <div className="ov-main">
        <section className="panel chart-panel" aria-label="Расход кредитов">
          <div className="panel-head">
            <h2>Расход кредитов</h2>
            <ul className="legend">
              <li><i className="lg-spend" />за каждые {span || '—'}</li>
              <li><i className="lg-peak" />пик</li>
              <li><i className="lg-average" />среднее по активным</li>
              {unsure && (
                <li>
                  <svg className="lg-unsure" width="14" height="4" viewBox="0 0 14 4" aria-hidden="true"><path d="M0 2H14" /></svg>
                  стоимость сообщена не полностью
                </li>
              )}
            </ul>
          </div>
          <SpendChart buckets={buckets} windowSec={windowSec} coveredFrom={coverage?.from ?? null} />
        </section>
        <div className="ov-side">
          <section className="panel" aria-label="Модели">
            <div className="panel-head">
              <h2>Модели</h2>
              <span className="panel-meta">ходов {fmtInt(totals.turns)} · инструментов {fmtInt(totals.tools)}</span>
            </div>
            <RankedList items={models} empty="За окно ходов не было" />
          </section>
          <CacheFacts totals={totals} />
        </div>
      </div>

      <section className="panel" aria-label="Кэш промпта">
          <div className="panel-head">
            <h2>Кэш промпта</h2>
            <ul className="legend">
              <li><i className="lg-read-line" />чтение</li>
              <li><i className="lg-write-line" />запись</li>
              <li><i className="lg-spike" />всплеск записи</li>
            </ul>
          </div>
          <CacheChart buckets={buckets} windowSec={windowSec} coveredFrom={coverage?.from ?? null} />
        </section>
    </Page>
  );
}
