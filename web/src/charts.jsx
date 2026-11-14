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

function useHover(n, plotW) {
  const [hover, setHover] = useState(null);
  const onMove = (event) => {
    if (!n) return;
    const box = event.currentTarget.getBoundingClientRect();
    const i = Math.round(((event.clientX - box.left - PAD.l) / plotW) * (n - 1));
    setHover(i < 0 || i >= n ? null : i);
  };
  const keyboard = {
    tabIndex: n ? 0 : -1,
    onFocus: () => setHover((current) => current ?? 0),
    onBlur: () => setHover(null),
    onKeyDown: (event) => {
      if (!n || !['ArrowLeft', 'ArrowRight', 'Home', 'End', 'Escape'].includes(event.key)) return;
      event.preventDefault();
      setHover((current) => event.key === 'Escape' ? null : event.key === 'Home' ? 0 : event.key === 'End' ? n - 1
        : Math.min(n - 1, Math.max(0, (current ?? 0) + (event.key === 'ArrowRight' ? 1 : -1))));
    },
  };
  return [hover, onMove, () => setHover(null), keyboard];
}

function Tip({ W, x, children }) {
  const flip = x > W - 220;
  return <dl className="chart-tip" role="status" aria-live="polite" style={flip ? { right: W - x + 12 } : { left: x + 12 }}>{children}</dl>;
}

/**
 * Monotone cubic through the points (Fritsch–Carlson): smooth, but it never
 * overshoots, so a quiet minute stays at zero instead of dipping below the axis.
 */
function smooth(points) {
  const n = points.length;
  if (!n) return '';
  if (n === 1) return `M${points[0][0]},${points[0][1]}`;
  const dx = [], slope = [];
  for (let i = 0; i < n - 1; i += 1) {
    dx.push(points[i + 1][0] - points[i][0]);
    slope.push((points[i + 1][1] - points[i][1]) / (dx[i] || 1));
  }
  const t = [slope[0]];
  for (let i = 1; i < n - 1; i += 1) t.push(slope[i - 1] * slope[i] <= 0 ? 0 : (slope[i - 1] + slope[i]) / 2);
  t.push(slope[n - 2]);
  for (let i = 0; i < n - 1; i += 1) {
    if (slope[i] === 0) { t[i] = 0; t[i + 1] = 0; continue; }
    const a = t[i] / slope[i], b = t[i + 1] / slope[i];
    const h = a * a + b * b;
    if (h > 9) { const k = 3 / Math.sqrt(h); t[i] = k * a * slope[i]; t[i + 1] = k * b * slope[i]; }
  }
  let d = `M${points[0][0].toFixed(1)},${points[0][1].toFixed(1)}`;
  for (let i = 0; i < n - 1; i += 1) {
    const [x0, y0] = points[i], [x1, y1] = points[i + 1], h = dx[i] / 3;
    d += `C${(x0 + h).toFixed(1)},${(y0 + t[i] * h).toFixed(1)} ${(x1 - h).toFixed(1)},${(y1 - t[i + 1] * h).toFixed(1)} ${x1.toFixed(1)},${y1.toFixed(1)}`;
  }
  return d;
}

const areaUnder = (line, points, baseY) => (points.length
  ? `${line}L${points[points.length - 1][0].toFixed(1)},${baseY}L${points[0][0].toFixed(1)},${baseY}Z` : '');

/** Credits spent per bucket as a line: it climbs with a heavy turn and falls back with a light one. */
export function SpendChart({ buckets, windowSec }) {
  const [ref, size] = useSize();
  const n = buckets.length;
  const W = size.w;
  const H = Math.max(size.h, 200);
  const plotW = Math.max(10, W - PAD.l - PAD.r);
  const plotH = H - PAD.t - PAD.b;
  const [hover, onMove, onLeave, keyboard] = useHover(n, plotW);

  const model = useMemo(() => {
    const values = buckets.map((b) => Number(b.credits) || 0);
    const peak = Math.max(0, ...values);
    const total = values.reduce((s, v) => s + v, 0);
    const busy = values.filter((v) => v > 0).length;
    return { values, peak, peakIndex: values.indexOf(peak), total, max: niceMax(peak), average: busy ? total / busy : 0 };
  }, [buckets]);

  const empty = !n || !model.total;
  const incomplete = buckets.some((b) => b.credits_complete === false);
  const baseY = PAD.t + plotH + 0.5;
  const yAt = (v) => baseY - (v / model.max) * plotH;
  const xAt = (i) => PAD.l + (n > 1 ? (i / (n - 1)) * plotW : plotW / 2);
  const points = model.values.map((v, i) => [xAt(i), yAt(v)]);
  const line = smooth(points);
  const ticks = [model.max / 2, model.max];
  const labels = timeLabels(buckets, plotW);
  const active = hover !== null && buckets[hover] ? hover : null;
  const bucket = active !== null ? buckets[active] : null;
  const span = bucketLabel(windowSec, n);
  const peakRight = !empty && xAt(model.peakIndex) > W - 120;
  const summary = empty ? incomplete ? 'Стоимость не сообщена' : 'Расхода за окно не было'
    : `Расход кредитов за каждые ${span}: всего ${fmtFixed(model.total, 2)}, максимум ${fmtFixed(model.peak, 2)}`;

  return (
    <div className="chart-frame" ref={ref}>
      <svg className="chart" width={W} height={H} role="img" aria-label={summary} aria-description="Стрелки влево и вправо выбирают период, Home и End — крайние периоды" {...keyboard} onPointerMove={onMove} onPointerLeave={onLeave}>
        {ticks.map((v) => (
          <g key={v}>
            <line className="c-grid" x1={PAD.l} x2={W - PAD.r} y1={Math.round(yAt(v)) + 0.5} y2={Math.round(yAt(v)) + 0.5} />
            <text className="c-label" x={PAD.l - 8} y={yAt(v) + 4} textAnchor="end">{fmtFixed(v, v < 1 ? 2 : 1)}</text>
          </g>
        ))}
        <text className="c-label" x={PAD.l - 8} y={baseY + 4} textAnchor="end">0</text>
        {!empty && (
          <>
            <path className="c-spend-area" d={areaUnder(line, points, baseY)} />
            <path className="c-spend-line" d={line} />
            {model.average > 0 && (
              <line className="c-average" x1={PAD.l} x2={W - PAD.r} y1={Math.round(yAt(model.average)) + 0.5} y2={Math.round(yAt(model.average)) + 0.5} />
            )}
            <circle className="c-peak" cx={xAt(model.peakIndex)} cy={yAt(model.peak)} r={4} />
            {/* The figure is dropped when the scale's top tick already says it */}
            {fmtFixed(model.peak, model.max < 1 ? 2 : 1) !== fmtFixed(model.max, model.max < 1 ? 2 : 1) && (
              <text className="c-label c-label-accent" x={xAt(model.peakIndex) + (peakRight ? -8 : 8)} y={yAt(model.peak) - 8}
                textAnchor={peakRight ? 'end' : 'start'}>{fmtFixed(model.peak, 2)}</text>
            )}
          </>
        )}
        <line className="c-axis" x1={PAD.l} x2={W - PAD.r} y1={baseY} y2={baseY} />
        {labels.map((i, j) => (
          <text key={`t${i}${j}`} className="c-label" x={xAt(i)} y={H - 8}
            textAnchor={j === 0 ? 'start' : j === labels.length - 1 ? 'end' : 'middle'}>{fmtShortTime(buckets[i].t)}</text>
        ))}
        {bucket && (
          <g>
            <line className="c-cursor" x1={xAt(active)} x2={xAt(active)} y1={PAD.t} y2={baseY} />
            <circle className="c-hover-dot" cx={xAt(active)} cy={yAt(model.values[active])} r={3.5} />
          </g>
        )}
      </svg>
      {empty && <p className="chart-empty">{incomplete ? 'Стоимость не сообщена' : 'Расхода за окно не было'}</p>}
      {bucket && (
        <Tip W={W} x={xAt(active)}>
          <dt className="mono">{fmtShortTime(bucket.t)}–{fmtShortTime(bucket.t + windowSec / n)}</dt>
          <dd><span>кредиты</span><b className="num">{bucket.credits_complete === false ? bucket.credits > 0 ? `≥ ${fmtFixed(bucket.credits, 2)}` : '—' : fmtFixed(bucket.credits, 2)}</b></dd>
          <dd><span>запросы</span><b className="num">{fmtInt(bucket.requests)}</b></dd>
          <dd><span>ходы</span><b className="num">{fmtInt(bucket.turns)}</b></dd>
          <dd><span>первый токен</span><b className="num">{fmtMs(bucket.ttft)}</b></dd>
        </Tip>
      )}
    </div>
  );
}

/** A write is a spike when it is several times the window's usual write; cause is not inferred. */
export function writeSpikes(buckets) {
  const writes = buckets.map((b) => b.cache_write || 0);
  const busy = writes.filter((w) => w > 0).sort((a, b) => a - b);
  if (!busy.length) return new Set();
  const median = busy[Math.floor(busy.length / 2)];
  const threshold = Math.max(median * 4, 20000);
  return new Set(writes.flatMap((w, i) => (w >= threshold ? [i] : [])));
}

/**
 * Prompt cache as two lanes on one time axis: reads on top, writes below, each
 * on its own scale. On a shared scale reads (millions) flatten writes to the
 * axis; here a sudden write, the expensive moment, stands out and is marked.
 */
export function CacheChart({ buckets, windowSec }) {
  const [ref, size] = useSize();
  const n = buckets.length;
  const W = size.w;
  const H = Math.max(size.h, 200);
  const plotW = Math.max(10, W - PAD.l - PAD.r);
  const gap = 20;
  const laneH = (H - PAD.t - PAD.b - gap) / 2;
  const [hover, onMove, onLeave, keyboard] = useHover(n, plotW);

  const model = useMemo(() => {
    const reads = buckets.map((b) => b.cache_read || 0);
    const writes = buckets.map((b) => b.cache_write || 0);
    return { reads, writes, readMax: niceMax(Math.max(0, ...reads)), writeMax: niceMax(Math.max(0, ...writes)), spikes: writeSpikes(buckets) };
  }, [buckets]);

  const readBase = PAD.t + laneH + 0.5;
  const writeTop = PAD.t + laneH + gap;
  const writeBase = writeTop + laneH + 0.5;
  const xAt = (i) => PAD.l + (n > 1 ? (i / (n - 1)) * plotW : plotW / 2);
  const readY = (v) => readBase - (v / model.readMax) * (laneH - 4);
  const writeY = (v) => writeBase - (v / model.writeMax) * (laneH - 4);
  const readPts = model.reads.map((v, i) => [xAt(i), readY(v)]);
  const writePts = model.writes.map((v, i) => [xAt(i), writeY(v)]);
  const readLine = smooth(readPts);
  const writeLine = smooth(writePts);
  const empty = !n || (!Math.max(0, ...model.reads) && !Math.max(0, ...model.writes));
  const labels = timeLabels(buckets, plotW);
  const active = hover !== null && buckets[hover] ? hover : null;
  const bucket = active !== null ? buckets[active] : null;
  const spikes = [...model.spikes];
  const summary = empty ? 'Обращений к кэшу за окно не было'
    : `Кэш за окно: прочитано ${fmtTokens(model.reads.reduce((s, v) => s + v, 0))}, записано ${fmtTokens(model.writes.reduce((s, v) => s + v, 0))}`
      + (spikes.length ? `, всплесков записи: ${spikes.length}` : '');

  return (
    <div className="chart-frame chart-frame-cache" ref={ref}>
      <svg className="chart" width={W} height={H} role="img" aria-label={summary} aria-description="Стрелки влево и вправо выбирают период, Home и End — крайние периоды" {...keyboard} onPointerMove={onMove} onPointerLeave={onLeave}>
        <text className="c-label" x={PAD.l - 8} y={PAD.t + 8} textAnchor="end">{fmtTokens(model.readMax)}</text>
        <text className="c-label" x={PAD.l - 8} y={writeTop + 8} textAnchor="end">{fmtTokens(model.writeMax)}</text>
        <text className="c-lane" x={PAD.l + 4} y={PAD.t + 10}>чтение</text>
        <text className="c-lane" x={PAD.l + 4} y={writeTop + 10}>запись</text>
        {!empty && (
          <>
            <path className="c-read-area" d={areaUnder(readLine, readPts, readBase)} />
            <path className="c-read-line" d={readLine} />
            <path className="c-write-area" d={areaUnder(writeLine, writePts, writeBase)} />
            <path className="c-write-line" d={writeLine} />
            {(() => {
              // Lay the labels out left to right by their real extent: a label goes right of its dot,
              // or left of it near the edge, and is dropped when it would touch the previous one
              let lastRight = -Infinity;
              return spikes.map((i) => {
                const text = `+${fmtTokens(model.writes[i])}`;
                const width = text.length * 6.6;
                const x = xAt(i);
                const boxes = [[x + 8, x + 8 + width, 'start'], [x - 8 - width, x - 8, 'end']]
                  .filter(([left, right]) => left >= PAD.l && right <= W - 4 && left > lastRight + 8
                    && !spikes.some((j) => j !== i && xAt(j) > left - 6 && xAt(j) < right + 6));
                const box = boxes[0];
                if (box) lastRight = box[1];
                return (
                  <g key={`s${i}`} className="c-spike">
                    <circle cx={x} cy={writeY(model.writes[i])} r={4} />
                    {box && (
                      <text className="c-label c-label-accent" x={box[2] === 'start' ? box[0] : box[1]} y={writeY(model.writes[i]) + 4}
                        textAnchor={box[2]}>{text}</text>
                    )}
                  </g>
                );
              });
            })()}
          </>
        )}
        <line className="c-axis" x1={PAD.l} x2={W - PAD.r} y1={readBase} y2={readBase} />
        <line className="c-axis" x1={PAD.l} x2={W - PAD.r} y1={writeBase} y2={writeBase} />
        {labels.map((i, j) => (
          <text key={`t${i}${j}`} className="c-label" x={xAt(i)} y={H - 8}
            textAnchor={j === 0 ? 'start' : j === labels.length - 1 ? 'end' : 'middle'}>{fmtShortTime(buckets[i].t)}</text>
        ))}
        {bucket && (
          <g>
            <line className="c-cursor" x1={xAt(active)} x2={xAt(active)} y1={PAD.t} y2={writeBase} />
            <circle className="c-hover-dot" cx={xAt(active)} cy={readY(model.reads[active])} r={3.5} />
            <circle className="c-hover-dot" cx={xAt(active)} cy={writeY(model.writes[active])} r={3.5} />
          </g>
        )}
      </svg>
      {empty && <p className="chart-empty">Обращений к кэшу за окно не было</p>}
      {bucket && (
        <Tip W={W} x={xAt(active)}>
          <dt className="mono">{fmtShortTime(bucket.t)}–{fmtShortTime(bucket.t + windowSec / n)}</dt>
          <dd><span>прочитано</span><b className="num">{fmtTokens(bucket.cache_read)}</b></dd>
          <dd><span>записано</span><b className="num">{fmtTokens(bucket.cache_write)}</b></dd>
          <dd><span>без кэша</span><b className="num">{fmtTokens(bucket.input)}</b></dd>
          {model.spikes.has(active) && <dd className="tip-note">запись выше обычной за окно</dd>}
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
export function TurnTape({ trace, max, now, className = '' }) {
  const p = phasesOf(trace, now);
  const values = [p.wait, p.think, p.tools, p.out, p.other];
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
