import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import { isNum, fmtShortTime, fmtTokens, fmtFixed, fmtInt, fmtMs } from './format.js';
import { PHASES, phasesOf } from './turns.js';

function useSize() {
  const ref = useRef(null);
  const [size, setSize] = useState({ w: 640, h: 300 });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      setSize({ w: Math.round(width), h: Math.round(height) });
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, size];
}

function niceMax(value) {
  if (!(value > 0)) return 1;
  const pow = 10 ** Math.floor(Math.log10(value));
  const f = value / pow;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * pow;
}

const PAD = { l: 56, r: 16, t: 12, b: 28 };

/** The bucket length in words: "1 мин", "5 мин", "15 мин". */
export function bucketLabel(windowSec, count) {
  if (!count) return '';
  const seconds = Math.round(windowSec / count);
  return seconds % 60 === 0 ? `${seconds / 60}\u00a0мин` : `${seconds}\u00a0с`;
}

function timeLabels(buckets, plotW) {
  const n = buckets.length;
  const fractions = plotW < 420 ? [0, 0.5, 1] : [0, 0.25, 0.5, 0.75, 1];
  return n ? fractions.map((f) => Math.min(n - 1, Math.round(f * (n - 1)))) : [];
}

function useHover(n, step) {
  const [hover, setHover] = useState(null);
  const onMove = (event) => {
    if (!n) return;
    const box = event.currentTarget.getBoundingClientRect();
    const i = Math.floor((event.clientX - box.left - PAD.l) / step);
    setHover(i < 0 || i >= n ? null : i);
  };
  return [hover, onMove, () => setHover(null)];
}

function Tip({ W, x, children }) {
  const flip = x > W - 220;
  return <dl className="chart-tip" style={flip ? { right: W - x + 12 } : { left: x + 12 }}>{children}</dl>;
}

/** Credits spent in each bucket: bars rise with a heavy turn and fall back with a light one. */
export function SpendChart({ buckets, windowSec }) {
  const [ref, size] = useSize();
  const n = buckets.length;
  const W = size.w;
  const H = Math.max(size.h, 200);
  const plotW = Math.max(10, W - PAD.l - PAD.r);
  const plotH = H - PAD.t - PAD.b;
  const step = n ? plotW / n : plotW;
  const [hover, onMove, onLeave] = useHover(n, step);

  const model = useMemo(() => {
    const values = buckets.map((b) => Number(b.credits) || 0);
    const peak = Math.max(0, ...values);
    const total = values.reduce((s, v) => s + v, 0);
    const busy = values.filter((v) => v > 0).length;
    return { values, peak, peakIndex: values.indexOf(peak), total, max: niceMax(peak), average: busy ? total / busy : 0 };
  }, [buckets]);

  const empty = !n || !model.total;
  const baseY = PAD.t + plotH + 0.5;
  const yAt = (v) => baseY - (v / model.max) * plotH;
  const barW = Math.max(1, step * 0.7);
  const xAt = (i) => PAD.l + (i + 0.5) * step;
  const ticks = [model.max / 2, model.max];
  const labels = timeLabels(buckets, plotW);
  const active = hover !== null && buckets[hover] ? hover : null;
  const bucket = active !== null ? buckets[active] : null;
  const span = bucketLabel(windowSec, n);
  const summary = empty ? 'Расхода за окно не было'
    : `Расход кредитов за каждые ${span}: всего ${fmtFixed(model.total, 2)}, максимум ${fmtFixed(model.peak, 2)}`;

  return (
    <div className="chart-frame" ref={ref}>
      <svg className="chart" width={W} height={H} role="img" aria-label={summary} onPointerMove={onMove} onPointerLeave={onLeave}>
        {ticks.map((v) => (
          <g key={v}>
            <line className="c-grid" x1={PAD.l} x2={W - PAD.r} y1={Math.round(yAt(v)) + 0.5} y2={Math.round(yAt(v)) + 0.5} />
            <text className="c-label" x={PAD.l - 8} y={yAt(v) + 4} textAnchor="end">{fmtFixed(v, v < 1 ? 2 : 1)}</text>
          </g>
        ))}
        <text className="c-label" x={PAD.l - 8} y={baseY + 4} textAnchor="end">0</text>
        {!empty && model.values.map((v, i) => (v > 0 ? (
          <rect key={buckets[i].t} className={`c-spend${i === model.peakIndex ? ' peak' : ''}${i === active ? ' on' : ''}`}
            x={xAt(i) - barW / 2} y={yAt(v)} width={barW} height={Math.max(1, baseY - yAt(v))} />
        ) : null))}
        {!empty && model.average > 0 && (
          <g>
            <line className="c-average" x1={PAD.l} x2={W - PAD.r} y1={Math.round(yAt(model.average)) + 0.5} y2={Math.round(yAt(model.average)) + 0.5} />
          </g>
        )}
        <line className="c-axis" x1={PAD.l} x2={W - PAD.r} y1={baseY} y2={baseY} />
        {labels.map((i, j) => (
          <text key={`t${i}${j}`} className="c-label" x={xAt(i)} y={H - 8}
            textAnchor={j === 0 ? 'start' : j === labels.length - 1 ? 'end' : 'middle'}>{fmtShortTime(buckets[i].t)}</text>
        ))}
        {bucket && <line className="c-cursor" x1={xAt(active)} x2={xAt(active)} y1={PAD.t} y2={baseY} />}
      </svg>
      {empty && <p className="chart-empty">Расхода за окно не было</p>}
      {bucket && (
        <Tip W={W} x={xAt(active)}>
          <dt className="mono">{fmtShortTime(bucket.t)}–{fmtShortTime(bucket.t + windowSec / n)}</dt>
          <dd><span>кредиты</span><b className="num">{fmtFixed(bucket.credits, 2)}</b></dd>
          <dd><span>запросы</span><b className="num">{fmtInt(bucket.requests)}</b></dd>
          <dd><span>ходы</span><b className="num">{fmtInt(bucket.turns)}</b></dd>
          <dd><span>первый токен</span><b className="num">{fmtMs(bucket.ttft)}</b></dd>
        </Tip>
      )}
    </div>
  );
}

/** Prompt cache per bucket: read above the axis, write below, on one token scale. */
export function CacheChart({ buckets, windowSec }) {
  const [ref, size] = useSize();
  const n = buckets.length;
  const W = size.w;
  const H = Math.max(size.h, 160);
  const plotW = Math.max(10, W - PAD.l - PAD.r);
  const plotH = H - PAD.t - PAD.b;
  const step = n ? plotW / n : plotW;
  const [hover, onMove, onLeave] = useHover(n, step);

  const model = useMemo(() => {
    const maxRead = Math.max(0, ...buckets.map((b) => b.cache_read || 0));
    const maxWrite = Math.max(0, ...buckets.map((b) => b.cache_write || 0));
    const readMax = niceMax(maxRead);
    const writeMax = niceMax(maxWrite);
    const split = Math.max(0.6, Math.min(0.86, readMax / (readMax + writeMax)));
    return { maxRead, maxWrite, readMax, writeMax, split };
  }, [buckets]);

  const axisY = Math.round(PAD.t + plotH * model.split) + 0.5;
  const readH = axisY - PAD.t;
  const writeH = PAD.t + plotH - axisY;
  const k = Math.min(readH / model.readMax, writeH / model.writeMax);
  const empty = !n || (!model.maxRead && !model.maxWrite);
  const barW = Math.max(1, step * 0.7);
  const xAt = (i) => PAD.l + (i + 0.5) * step;
  const labels = timeLabels(buckets, plotW);
  const active = hover !== null && buckets[hover] ? hover : null;
  const bucket = active !== null ? buckets[active] : null;
  const summary = empty ? 'Обращений к кэшу за окно не было'
    : `Кэш за окно: прочитано ${fmtTokens(buckets.reduce((s, b) => s + (b.cache_read || 0), 0))}, `
      + `записано ${fmtTokens(buckets.reduce((s, b) => s + (b.cache_write || 0), 0))}`;

  return (
    <div className="chart-frame chart-frame-small" ref={ref}>
      <svg className="chart" width={W} height={H} role="img" aria-label={summary} onPointerMove={onMove} onPointerLeave={onLeave}>
        {model.readMax * k <= readH + 1 && (
          <g>
            <line className="c-grid" x1={PAD.l} x2={W - PAD.r} y1={Math.round(axisY - model.readMax * k) + 0.5} y2={Math.round(axisY - model.readMax * k) + 0.5} />
            <text className="c-label" x={PAD.l - 8} y={axisY - model.readMax * k + 4} textAnchor="end">{fmtTokens(model.readMax)}</text>
          </g>
        )}
        <text className="c-label" x={PAD.l - 8} y={axisY + 4} textAnchor="end">0</text>
        {!empty && buckets.map((b, i) => {
          const r = (b.cache_read || 0) * k;
          const w = (b.cache_write || 0) * k;
          return (
            <g key={b.t} className={i === active ? 'on' : undefined}>
              {r > 0 && <rect className="c-read" x={xAt(i) - barW / 2} y={axisY - r} width={barW} height={r} />}
              {w > 0 && <rect className="c-write" x={xAt(i) - barW / 2} y={axisY + 1} width={barW} height={w} />}
            </g>
          );
        })}
        <line className="c-axis" x1={PAD.l} x2={W - PAD.r} y1={axisY} y2={axisY} />
        {labels.map((i, j) => (
          <text key={`t${i}${j}`} className="c-label" x={xAt(i)} y={H - 8}
            textAnchor={j === 0 ? 'start' : j === labels.length - 1 ? 'end' : 'middle'}>{fmtShortTime(buckets[i].t)}</text>
        ))}
        {bucket && <line className="c-cursor" x1={xAt(active)} x2={xAt(active)} y1={PAD.t} y2={PAD.t + plotH} />}
      </svg>
      {empty && <p className="chart-empty">Обращений к кэшу за окно не было</p>}
      {bucket && (
        <Tip W={W} x={xAt(active)}>
          <dt className="mono">{fmtShortTime(bucket.t)}–{fmtShortTime(bucket.t + windowSec / n)}</dt>
          <dd><span>прочитано</span><b className="num">{fmtTokens(bucket.cache_read)}</b></dd>
          <dd><span>записано</span><b className="num">{fmtTokens(bucket.cache_write)}</b></dd>
          <dd><span>без кэша</span><b className="num">{fmtTokens(bucket.input)}</b></dd>
        </Tip>
      )}
    </div>
  );
}

/** Ranked list with thin bars: a different kind of data block than the chart. */
export function RankedList({ items, format = fmtInt, empty = 'Пока пусто' }) {
  const max = Math.max(1, ...items.map((item) => item.value));
  if (!items.length) return <p className="muted small">{empty}</p>;
  return (
    <ul className="rank">
      {items.map((item, index) => (
        <li key={item.label}>
          <span className="rank-label mono" title={item.label}>{item.label}</span>
          <span className="rank-value num">{format(item.value)}</span>
          <span className="rank-track"><span className={`rank-fill${index === 0 ? ' lead' : ''}`} style={{ width: `${(item.value / max) * 100}%` }} /></span>
        </li>
      ))}
    </ul>
  );
}

/** The turn tape: a thin strip of phases (waiting, thinking, tools, output) on a shared scale. */
export function TurnTape({ trace, max, className = '' }) {
  const p = phasesOf(trace);
  const values = [p.wait, p.think, p.tools, p.out];
  const fill = max ? Math.max(1.5, (p.total / max) * 100) : 100;
  const label = PHASES.map((phase, i) => `${phase.label} ${fmtMs(values[i])}`).join(', ');
  return (
    <span className={`tape ${trace.status === 'error' ? 'is-error' : ''} ${p.open ? 'is-open' : ''} ${className}`} role="img" aria-label={`Фазы хода: ${label}`}>
      <span className="tape-fill" style={{ width: `${Math.min(100, fill)}%` }}>
        {values.map((ms, i) => (ms > 0 ? <i key={PHASES[i].key} className={`ph-${i + 1}`} style={{ flexGrow: ms }} title={`${PHASES[i].label} ${fmtMs(ms)}`} /> : null))}
        {(p.open || trace.status === 'error') && <i className="tape-cap" />}
      </span>
    </span>
  );
}

export function TapeLegend() {
  return (
    <ul className="tape-legend" aria-label="Фазы хода">
      {PHASES.map((phase, i) => <li key={phase.key}><i className={`ph-${i + 1}`} />{phase.label}</li>)}
    </ul>
  );
}

export { isNum };
