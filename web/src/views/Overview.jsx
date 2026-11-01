import { useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Activity, Coins, Cpu, Database, Layers, RefreshCw, Server, Timer, TriangleAlert, Wifi, WifiOff, Zap } from 'lucide-react';
import { api } from '../api.js';
import { usePolling } from '../useJournal.js';
import { AnimatedNumber, Page, PulseDot, Reveal, spring } from '../motion.jsx';
import { AreaChart, Bars, Ring } from '../charts.jsx';
import { Badge, Button, CopyButton } from '../ui.jsx';
import { category } from '../catalog.js';
import { fmtAgo, fmtClock, fmtInt, fmtMs, fmtNum, fmtPercent, fmtTokens, fmtUptime, isNum } from '../format.js';

function Kpi({ icon: Icon, label, value, format, hint, tone = 'neutral' }) {
  return (
    <Reveal className={`kpi tone-${tone}`} whileHover={{ y: -4 }} transition={spring}>
      <span className="kpi-icon"><Icon size={18} /></span>
      <span className="kpi-label">{label}</span>
      <AnimatedNumber value={value} format={format} className="kpi-value" />
      {hint && <span className="kpi-hint">{hint}</span>}
    </Reveal>
  );
}

export default function Overview({ apiKey, journal, onUnauthorized, go }) {
  const [overview, setOverview] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [error, setError] = useState('');
  const [spin, setSpin] = useState(0);

  async function load() {
    try {
      const [o, m] = await Promise.all([api(apiKey, 'overview'), api(apiKey, 'metrics?window=3600')]);
      setOverview(o);
      setMetrics(m);
      setError('');
    } catch (failure) {
      if (failure.status === 401) onUnauthorized();
      else setError(failure.message);
    }
  }
  usePolling(load, 8000, [apiKey]);

  const totals = metrics?.totals || {};
  const buckets = metrics?.buckets || [];
  const balance = overview?.balance || {};
  const ok = overview?.health?.ok;
  const recent = journal.events.slice(-9).reverse();
  const allocation = balance.monthly_allocation;
  const left = isNum(balance.available) && allocation ? balance.available / allocation : null;
  const models = Object.entries(metrics?.models || {}).map(([label, value]) => ({ label, value }));

  return (
    <Page className="overview">
      <Reveal as="header" className="page-head">
        <div>
          <p className="eyebrow">Обзор · последний час</p>
          <h1>Всё под контролем.</h1>
        </div>
        <div className="head-actions">
          {overview && (
            <Badge tone={ok ? 'success' : 'danger'}>
              {ok ? <Wifi size={13} /> : <WifiOff size={13} />} upstream {ok ? 'ok' : 'degraded'}
            </Badge>
          )}
          <Button onClick={() => { setSpin((n) => n + 1); load(); }} aria-label="Обновить обзор">
            <motion.span key={spin} className="icon-swap" initial={{ rotate: 0 }} animate={{ rotate: spin ? 360 : 0 }}
              transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1] }}>
              <RefreshCw size={16} aria-hidden="true" />
            </motion.span>
            Обновить
          </Button>
        </div>
      </Reveal>

      <AnimatePresence>
        {(error || overview?.health?.error) && (
          <motion.div className="banner tone-danger" initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}>
            <TriangleAlert size={16} /> {error || overview.health.error}
          </motion.div>
        )}
      </AnimatePresence>

      <section className="hero-grid">
        <Reveal className="hero-card balance-card">
          <div className="hero-glow" aria-hidden="true" />
          <p className="eyebrow">Доступно кредитов</p>
          <AnimatedNumber value={balance.available} format={(v) => fmtNum(v, 2)} className="metric" />
          <p className="muted">
            из {fmtNum(allocation, 0)} в месяц · сброс {balance.reset_date || '—'}
            {balance.stale && <Badge tone="warn">устарело</Badge>}
          </p>
          <div className="meter" aria-label="Остаток месячного лимита">
            <motion.span initial={{ scaleX: 0 }} animate={{ scaleX: left ?? 0 }} transition={{ duration: 1.3, ease: [0.22, 1, 0.36, 1] }} />
          </div>
          <div className="hero-foot">
            <span><b className="num">{fmtNum(overview?.usage?.session_used, 3)}</b> потрачено с запуска</span>
            <span><b className="num">{fmtUptime(overview?.uptime_seconds)}</b> аптайм</span>
          </div>
        </Reveal>

        <Reveal className="hero-card cache-card">
          <p className="eyebrow">Попадание в кэш промпта</p>
          <Ring value={totals.cache_hit_ratio} label={fmtPercent(totals.cache_hit_ratio)} caption="за час" />
          <dl className="mini-stats">
            <div><dt>прочитано</dt><dd className="num">{fmtTokens(totals.cache_read)}</dd></div>
            <div><dt>записано</dt><dd className="num">{fmtTokens(totals.cache_write)}</dd></div>
            <div><dt>без кэша</dt><dd className="num">{fmtTokens(totals.input_tokens)}</dd></div>
          </dl>
        </Reveal>

        <Reveal className="hero-card route-card">
          <p className="eyebrow">Маршрут</p>
          <div className="route">
            {[{ icon: Cpu, label: 'Клиенты IDE' }, { icon: Server, label: 'relay :8787' }, { icon: Zap, label: 'Retool' }].map((node, index) => (
              <div className="route-node" key={node.label}>
                <motion.span className="route-icon" initial={{ scale: 0 }} animate={{ scale: 1 }} transition={{ ...spring, delay: 0.2 + index * 0.12 }}>
                  <node.icon size={18} />
                </motion.span>
                <span>{node.label}</span>
                {index < 2 && (
                  <span className="route-wire" aria-hidden="true">
                    <motion.i animate={{ x: ['-10%', '110%'] }} transition={{ duration: 1.6, repeat: Infinity, ease: 'easeInOut', delay: index * 0.4 }} />
                  </span>
                )}
              </div>
            ))}
          </div>
          <dl className="mini-stats">
            <div><dt>активно</dt><dd className="num">{fmtInt(overview?.sessions?.active)}</dd></div>
            <div><dt>диалогов</dt><dd className="num">{fmtInt(overview?.sessions?.cached)}</dd></div>
            <div><dt>режим</dt><dd>{overview?.sessions?.mode || '—'}</dd></div>
          </dl>
        </Reveal>
      </section>

      <section className="kpis">
        <Kpi icon={Activity} label="Запросов" value={totals.requests} format={fmtInt} hint={`${fmtInt(totals.errors)} ошибок`} tone={totals.errors ? 'warn' : 'neutral'} />
        <Kpi icon={Layers} label="Ходов модели" value={totals.turns} format={fmtInt} hint={`${fmtInt(totals.tools)} вызовов инструментов`} />
        <Kpi icon={Timer} label="TTFT p50" value={totals.ttft_p50} format={fmtMs} hint={`p95 ${fmtMs(totals.ttft_p95)}`} tone="accent" />
        <Kpi icon={Zap} label="Ответ p50" value={totals.latency_p50} format={fmtMs} hint={`p95 ${fmtMs(totals.latency_p95)}`} />
        <Kpi icon={Coins} label="Кредиты за час" value={totals.credits} format={(v) => fmtNum(v, 3)} />
        <Kpi icon={Database} label="Событий в буфере" value={metrics?.buffered} format={fmtInt} hint={`последнее #${fmtInt(metrics?.last_seq)}`} />
      </section>

      <section className="charts-grid">
        <Reveal className="panel">
          <div className="panel-head"><h2>Запросы в минуту</h2><Badge>{fmtInt(totals.requests)}</Badge></div>
          <AreaChart label="Запросы в минуту" points={buckets.map((b) => ({ t: b.t, v: b.requests }))} format={(v) => `${fmtInt(v)} запр.`} />
        </Reveal>
        <Reveal className="panel">
          <div className="panel-head"><h2>Время до первого токена</h2><Badge tone="accent">{fmtMs(totals.ttft_p50)}</Badge></div>
          <AreaChart label="Время до первого токена" tone="info" points={buckets.map((b) => ({ t: b.t, v: b.ttft }))} format={fmtMs} />
        </Reveal>
        <Reveal className="panel">
          <div className="panel-head"><h2>Кредиты</h2><Badge>{fmtNum(totals.credits, 3)}</Badge></div>
          <AreaChart label="Кредиты в минуту" tone="warn" points={buckets.map((b) => ({ t: b.t, v: b.credits }))} format={(v) => fmtNum(v, 4)} />
        </Reveal>
        <Reveal className="panel">
          <div className="panel-head"><h2>Модели</h2><Badge>{models.length}</Badge></div>
          <Bars items={models} format={fmtInt} empty="Ходов за час не было" />
        </Reveal>
      </section>

      <section className="split">
        <Reveal className="panel live-feed">
          <div className="panel-head">
            <h2><PulseDot tone={journal.status === 'live' ? 'success' : 'warn'} live={journal.status === 'live'} /> Прямо сейчас</h2>
            <Button onClick={() => go('journal')}>Весь журнал</Button>
          </div>
          <ul className="feed">
            <AnimatePresence initial={false}>
              {recent.map((event) => {
                const Icon = category(event.category).icon;
                return (
                  <motion.li key={event.seq} layout className={`feed-row level-${event.level}`}
                    initial={{ opacity: 0, x: -24, height: 0 }} animate={{ opacity: 1, x: 0, height: 'auto' }}
                    exit={{ opacity: 0, height: 0 }} transition={spring}>
                    <span className="feed-icon"><Icon size={14} /></span>
                    <span className="feed-msg">{event.message}</span>
                    <time className="mono muted" title={fmtClock(event.ts)}>{fmtAgo(event.ts)}</time>
                  </motion.li>
                );
              })}
            </AnimatePresence>
            {!recent.length && <li className="muted small">Событий пока нет — отправьте запрос в relay.</li>}
          </ul>
        </Reveal>
        <Reveal className="panel connect">
          <div className="panel-head"><h2>Подключение IDE</h2></div>
          {[['OpenAI base URL', overview?.connection?.openai_base_url], ['Anthropic base URL', overview?.connection?.anthropic_base_url]].map(([label, value]) => (
            <div className="connect-row" key={label}>
              <span className="muted small">{label}</span>
              <code>{value || '—'}</code>
              {value && <CopyButton value={value} label={`Копировать ${label}`} />}
            </div>
          ))}
          <div className="connect-row">
            <span className="muted small">Модель по умолчанию</span>
            <code>{overview?.models?.default || '—'}</code>
          </div>
          <p className="muted small">Ключ — тот же relay key: <code>Authorization: Bearer</code> или <code>x-api-key</code>.</p>
          <div className="session-state">
            <PulseDot tone={overview?.session?.error ? 'danger' : 'success'} live={!!overview?.session?.keepalive} />
            <span>Сессия Retool: {overview?.session?.error ? overview.session.error : `продлена ${fmtAgo(overview?.session?.last_success_at)}`}</span>
          </div>
        </Reveal>
      </section>
    </Page>
  );
}
