import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import { isNum, fmtShortTime, fmtTokens, fmtNum, fmtFixed, fmtInt, fmtMs } from './format.js';
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

function smooth(points) {
  if (!points.length) return '';
  let d = `M${points[0].x.toFixed(1)},${points[0].y.toFixed(1)}`;
  for (let i = 1; i < points.length; i += 1) {
    const a = points[i - 1];
    const b = points[i];
    const mid = (a.x + b.x) / 2;
    d += ` C${mid.toFixed(1)},${a.y.toFixed(1)} ${mid.toFixed(1)},${b.y.toFixed(1)} ${b.x.toFixed(1)},${b.y.toFixed(1)}`;
  }
  return d;
}

const PAD = { l: 60, r: 16, t: 12, b: 28 };

/** Mirrored cache chart: read above the axis, write below, credits as a thin line. */
export function CacheChart({ buckets, windowSec }) {
  const [ref, size] = useSize();
  const [hover, setHover] = useState(null);
  const n = buckets.length;
  const W = size.w;
  const H = Math.max(size.h, 240);
  const plotW = Math.max(10, W - PAD.l - PAD.r);
  const plotH = H - PAD.t - PAD.b;
  const step = n ? plotW / n : plotW;

  const model = useMemo(() => {
    const maxRead = Math.max(0, ...buckets.map((b) => b.cache_read || 0));
    const maxWrite = Math.max(0, ...buckets.map((b) => b.cache_write || 0));
    const readMax = niceMax(maxRead);
    const writeMax = niceMax(maxWrite);
    // Both halves share one token scale; the axis sits where that scale puts it,
    // so a quiet write side takes little room instead of an empty band.
    const share = readMax / (readMax + writeMax);
    const split = Math.max(0.6, Math.min(0.86, share));
    let running = 0;
    const cumulative = buckets.map((b) => (running += Number(b.credits) || 0));
    return { maxRead, maxWrite, readMax, writeMax, split, cumulative, total: running };
  }, [buckets]);

  const axisY = Math.round(PAD.t + plotH * model.split) + 0.5;
  const readH = axisY - PAD.t;
  const writeH = PAD.t + plotH - axisY;
  const k = Math.min(readH / model.readMax, writeH / model.writeMax);
  const empty = !n || (!model.maxRead && !model.maxWrite && !model.total);
  const barW = Math.max(1, step * 0.72);
  const xAt = (i) => PAD.l + (i + 0.5) * step;
  // Credits as a running total: the burn over the window, rising to the window's spend.
  const creditPts = model.cumulative.map((value, i) => ({
    x: xAt(i), y: axisY - 2 - (model.total ? (value / model.total) * (readH - 12) : 0),
  }));
  const readTicks = [model.readMax / 2, model.readMax].filter((v) => v * k <= readH + 1);
  const writeTicks = [model.writeMax].filter((v) => v * k <= writeH + 1 && v * k >= 14);
  // Fewer time labels on narrow charts, so neighbours never touch.
  const fractions = plotW < 420 ? [0, 0.5, 1] : [0, 0.25, 0.5, 0.75, 1];
  const labelIdx = n ? fractions.map((f) => Math.min(n - 1, Math.round(f * (n - 1)))) : [];
  const active = hover !== null && buckets[hover] ? hover : null;
  const bucket = active !== null ? buckets[active] : null;

  const summary = empty ? 'Нет данных за окно'
    : `Кэш за окно: прочитано ${fmtTokens(buckets.reduce((s, b) => s + (b.cache_read || 0), 0))}, `
      + `записано ${fmtTokens(buckets.reduce((s, b) => s + (b.cache_write || 0), 0))}, `
      + `кредитов ${fmtFixed(buckets.reduce((s, b) => s + (b.credits || 0), 0), 2)}`;

  function onMove(event) {
    if (!n) return;
    const box = event.currentTarget.getBoundingClientRect();
    const i = Math.floor((event.clientX - box.left - PAD.l) / step);
    setHover(i < 0 || i >= n ? null : i);
  }

  const tipLeft = bucket ? xAt(active) : 0;
  const flip = tipLeft > W - 220;

  return (
    <div className="chart-frame" ref={ref}>
      <svg className="chart" width={W} height={H} role="img" aria-label={summary}
        onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
        {readTicks.map((v) => (
          <g key={`r${v}`}>
            <line className="c-grid" x1={PAD.l} x2={W - PAD.r} y1={Math.round(axisY - v * k) + 0.5} y2={Math.round(axisY - v * k) + 0.5} />
            <text className="c-label" x={PAD.l - 8} y={axisY - v * k + 4} textAnchor="end">{fmtTokens(v)}</text>
          </g>
        ))}
        {writeTicks.map((v) => (
          <g key={`w${v}`}>
            <line className="c-grid" x1={PAD.l} x2={W - PAD.r} y1={Math.round(axisY + v * k) + 0.5} y2={Math.round(axisY + v * k) + 0.5} />
            <text className="c-label" x={PAD.l - 8} y={axisY + v * k + 4} textAnchor="end">{fmtTokens(v)}</text>
          </g>
        ))}
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
        {!empty && <path className="c-credits" d={smooth(creditPts)} />}
        {labelIdx.map((i, j) => (
          <text key={`t${i}${j}`} className="c-label" x={xAt(i)} y={H - 8}
            textAnchor={j === 0 ? 'start' : j === labelIdx.length - 1 ? 'end' : 'middle'}>
            {fmtShortTime(buckets[i].t)}
          </text>
        ))}
        {bucket && (
          <g>
            <line className="c-cursor" x1={xAt(active)} x2={xAt(active)} y1={PAD.t} y2={PAD.t + plotH} />
            <circle className="c-dot" cx={creditPts[active].x} cy={creditPts[active].y} r="3.5" />
          </g>
        )}
      </svg>
      {empty && <p className="chart-empty">Нет данных за окно</p>}
      {bucket && (
        <dl className="chart-tip" style={flip ? { right: W - tipLeft + 12 } : { left: tipLeft + 12 }}>
          <dt className="mono">{fmtShortTime(bucket.t)}–{fmtShortTime(bucket.t + windowSec / n)}</dt>
          <dd><span>чтение кэша</span><b className="num">{fmtTokens(bucket.cache_read)}</b></dd>
          <dd><span>запись кэша</span><b className="num">{fmtTokens(bucket.cache_write)}</b></dd>
          <dd><span>кредиты</span><b className="num">{fmtFixed(bucket.credits, 2)}</b></dd>
          <dd><span>с начала окна</span><b className="num">{fmtFixed(model.cumulative[active], 2)}</b></dd>
          <dd><span>запросы</span><b className="num">{fmtInt(bucket.requests)}</b></dd>
        </dl>
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
