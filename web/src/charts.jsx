import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import { isNum, fmtShortTime, fmtTokens, fmtFixed, fmtInt, fmtMs, plural } from './format.js';
import { uncoveredBuckets } from './coverage.js';
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

const STEPS = { ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1 };
const KEYS = new Set([...Object.keys(STEPS), 'Home', 'End', 'Escape']);

function focusVisible(element) {
  try { return element.matches(':focus-visible'); } catch { return true; }
}

/**
 * Which period is shown. The pointer and the keyboard each keep their own choice: when the pointer leaves, the
 * period goes back to the keyboard cursor instead of being wiped, and a key press takes over from the pointer.
 */
function useHover(n, plotW) {
  const [pointer, setPointer] = useState(null);
  const [cursor, setCursor] = useState(null);
  const onMove = (event) => {
    if (!n) return;
    const box = event.currentTarget.getBoundingClientRect();
    const i = Math.round(((event.clientX - box.left - PAD.l) / plotW) * (n - 1));
    setPointer(i < 0 || i >= n ? null : i);
  };
  const keyboard = n ? {
    tabIndex: 0,
    // Focus from the keyboard shows the first period; focus from a click leaves the choice to the pointer
    onFocus: (event) => { if (focusVisible(event.currentTarget)) setCursor((current) => current ?? 0); },
    onBlur: () => setCursor(null),
    onKeyDown: (event) => {
      if (!KEYS.has(event.key) || event.altKey || event.ctrlKey || event.metaKey) return;
      event.preventDefault();
      const from = pointer ?? cursor;
      setPointer(null);
      // With nothing chosen, an arrow enters from its own end: right starts at the first period, left at the last
      setCursor(event.key === 'Escape' ? null : event.key === 'Home' ? 0 : event.key === 'End' ? n - 1
        : from === null ? (STEPS[event.key] > 0 ? 0 : n - 1)
          : Math.min(n - 1, Math.max(0, from + STEPS[event.key])));
    },
  } : {};
  return [pointer ?? cursor, onMove, () => setPointer(null), keyboard];
}

const SLIDER_HINT = 'Стрелки выбирают период, Home и End — крайние периоды, Escape снимает выбор';

/** With periods the chart is a slider over them (one tab stop, the period read as its value); without, only a picture. */
function chartRole(n, active, text) {
  if (!n) return { role: 'img' };
  return { role: 'slider', 'aria-orientation': 'horizontal', 'aria-valuemin': 1, 'aria-valuemax': n, 'aria-valuenow': (active ?? 0) + 1,
    'aria-valuetext': active === null ? 'период не выбран' : text, 'aria-description': SLIDER_HINT };
}

const periodOf = (bucket, windowSec, n) => `${fmtShortTime(bucket.t)}–${fmtShortTime(bucket.t + windowSec / n)}`;
const spoken = (value) => String(value).replace('≥ ', 'не менее ').replace(/^—$/, 'нет данных');
const valueText = (period, rows) => `${period}: ${rows.map(([label, value, said]) => `${label} ${said ?? spoken(value)}`).join(', ')}`;

/** The tooltip repeats what the slider's value text says, so it stays out of the accessibility tree. */
function Tip({ W, x, period, rows, note }) {
  const flip = x > W - 220;
  return (
    <dl className="chart-tip" aria-hidden="true" style={flip ? { right: W - x + 12 } : { left: x + 12 }}>
      <dt className="mono">{period}</dt>
      {rows.map(([label, value]) => <dd key={label}><span>{label}</span><b className="num">{value}</b></dd>)}
      {note && <dd className="tip-note">{note}</dd>}
    </dl>
  );
}

/**
 * Monotone cubic through the points (Fritsch–Carlson): smooth, but it never
 * overshoots, so a quiet minute stays at zero instead of dipping below the axis.
 * Returns the Bézier command of every step from point i to point i + 1.
 */
function segments(points) {
  const n = points.length;
  if (n < 2) return [];
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
  return dx.map((step, i) => {
    const [x0, y0] = points[i], [x1, y1] = points[i + 1], h = step / 3;
    return `C${(x0 + h).toFixed(1)},${(y0 + t[i] * h).toFixed(1)} ${(x1 - h).toFixed(1)},${(y1 - t[i + 1] * h).toFixed(1)} ${x1.toFixed(1)},${y1.toFixed(1)}`;
  });
}

const moveTo = ([x, y]) => `M${x.toFixed(1)},${y.toFixed(1)}`;

function smooth(points) {
  if (!points.length) return '';
  if (points.length === 1) return `M${points[0][0]},${points[0][1]}`;
  return moveTo(points[0]) + segments(points).join('');
}

/** The curve cut into solid and dashed runs: a step touching a point flagged in `unsure` is dashed. */
function runs(points, unsure) {
  const result = [];
  segments(points).forEach((part, i) => {
    const dashed = Boolean(unsure[i] || unsure[i + 1]);
    const last = result[result.length - 1];
    if (last && last.dashed === dashed) last.d += part;
    else result.push({ dashed, d: moveTo(points[i]) + part });
  });
  return result;
}

const areaUnder = (line, points, baseY) => (points.length
  ? `${line}L${points[points.length - 1][0].toFixed(1)},${baseY}L${points[0][0].toFixed(1)},${baseY}Z` : '');

const diamond = (x, y, r = 4) => `M${x.toFixed(1)},${(y - r).toFixed(1)}L${(x + r).toFixed(1)},${y.toFixed(1)}L${x.toFixed(1)},${(y + r).toFixed(1)}L${(x - r).toFixed(1)},${y.toFixed(1)}Z`;

const spendRows = (bucket) => {
  const credits = bucket.credits_complete === false
    ? bucket.credits > 0 ? [`≥ ${fmtFixed(bucket.credits, 2)}`] : ['—', 'не сообщены'] : [fmtFixed(bucket.credits, 2)];
  // The credits already contain the cache refresh pings: their part is named, never added to the figure
  const warm = bucket.keepwarm_credits > 0 ? [['в т. ч. прогрев кэша', fmtFixed(bucket.keepwarm_credits, 2)]] : [];
  const unpriced = bucket.credits_unknown > 0 ? [['без цены', fmtInt(bucket.credits_unknown)]] : [];
  return [['кредиты', ...credits], ...warm, ...unpriced, ['запросы', fmtInt(bucket.requests)], ['ходы', fmtInt(bucket.turns)], ['первый токен', fmtMs(bucket.ttft)]];
};

const NO_DATA = 'нет данных: журнал начат позже';

/** The stretch of the window before the journal's coverage begins: no data, which is not the same as zero. */
function Uncovered({ x0, x1, y0, y1, labelY = y0 + 16 }) {
  return (
    <g className="c-uncovered">
      <rect x={x0} y={y0} width={Math.max(0, x1 - x0)} height={y1 - y0} />
      <line x1={x1} x2={x1} y1={y0} y2={y1} />
      {x1 - x0 > 90 && <text className="c-label" x={x0 + 8} y={labelY}>нет данных</text>}
    </g>
  );
}

/** Credits spent per bucket as a line: it climbs with a heavy turn and falls back with a light one. */
export function SpendChart({ buckets, windowSec, coveredFrom = null }) {
  const [ref, size] = useSize();
  const n = buckets.length;
  const W = size.w;
  const H = Math.max(size.h, 200);
  const plotW = Math.max(10, W - PAD.l - PAD.r);
  const plotH = H - PAD.t - PAD.b;
  const [hover, onMove, onLeave, keyboard] = useHover(n, plotW);
  const unseen = uncoveredBuckets(buckets, windowSec, coveredFrom);

  const model = useMemo(() => {
    // Nothing is known about the periods before the journal began: they are not spend, and not zero either
    const values = buckets.map((b, i) => (i < unseen ? 0 : Number(b.credits) || 0));
    // A bucket whose cost was not fully reported holds a lower bound, not a measurement
    const unsure = buckets.map((b, i) => i >= unseen && b.credits_complete === false);
    const peak = Math.max(0, ...values);
    const total = values.reduce((s, v) => s + v, 0);
    const busy = values.filter((v) => v > 0).length;
    return { values, unsure, unsureCount: unsure.filter(Boolean).length, peak, peakIndex: values.indexOf(peak), total,
      max: niceMax(peak), average: busy ? total / busy : 0 };
  }, [buckets, unseen]);

  const empty = !n || !model.total;
  const incomplete = model.unsureCount > 0;
  const baseY = PAD.t + plotH + 0.5;
  const yAt = (v) => baseY - (v / model.max) * plotH;
  const xAt = (i) => PAD.l + (n > 1 ? (i / (n - 1)) * plotW : plotW / 2);
  const points = model.values.map((v, i) => [xAt(i), yAt(v)]);
  const shown = points.slice(unseen);
  const line = smooth(shown);
  const ticks = [model.max / 2, model.max];
  const labels = timeLabels(buckets, plotW);
  const active = hover !== null && buckets[hover] ? hover : null;
  const bucket = active !== null ? buckets[active] : null;
  const blank = active !== null && active < unseen;
  const span = bucketLabel(windowSec, n);
  const peakRight = !empty && xAt(model.peakIndex) > W - 120;
  const peakUnsure = model.unsure[model.peakIndex];
  const bound = incomplete ? 'не менее ' : '';
  const since = unseen > 0 ? fmtShortTime(coveredFrom) : null;
  const emptyText = incomplete ? 'Стоимость не сообщена' : since ? `С ${since} расхода не было` : 'Расхода за окно не было';
  const summary = empty ? emptyText
    : `Расход кредитов за каждые ${span}: всего ${bound}${fmtFixed(model.total, 2)}, максимум ${bound}${fmtFixed(model.peak, 2)}`
      + (incomplete ? `. В ${model.unsureCount} ${plural(model.unsureCount, ['периоде', 'периодах', 'периодах'])} стоимость сообщена не полностью` : '')
      + (since ? `. Данные только с ${since}` : '');
  const period = bucket && periodOf(bucket, windowSec, n);
  const rows = bucket && !blank ? spendRows(bucket) : [];

  return (
    <div className="chart-frame" ref={ref}>
      <svg className="chart" width={W} height={H} aria-label={summary} {...keyboard}
        {...chartRole(n, active, bucket && (blank ? `${period}: ${NO_DATA}` : valueText(period, rows)))}
        onPointerMove={onMove} onPointerLeave={onLeave}>
        {unseen > 0 && <Uncovered x0={PAD.l} x1={xAt(unseen)} y0={PAD.t} y1={baseY} />}
        {ticks.map((v) => (
          <g key={v}>
            <line className="c-grid" x1={PAD.l} x2={W - PAD.r} y1={Math.round(yAt(v)) + 0.5} y2={Math.round(yAt(v)) + 0.5} />
            <text className="c-label" x={PAD.l - 8} y={yAt(v) + 4} textAnchor="end">{fmtFixed(v, v < 1 ? 2 : 1)}</text>
          </g>
        ))}
        <text className="c-label" x={PAD.l - 8} y={baseY + 4} textAnchor="end">0</text>
        {!empty && (
          <>
            <path className="c-spend-area" d={areaUnder(line, shown, baseY)} />
            {runs(shown, model.unsure.slice(unseen)).map((run, i) => (
              <path key={i} className={`c-spend-line${run.dashed ? ' is-unsure' : ''}`} d={run.d} />
            ))}
            {model.average > 0 && (
              <line className="c-average" x1={unseen > 0 ? xAt(unseen) : PAD.l} x2={W - PAD.r} y1={Math.round(yAt(model.average)) + 0.5} y2={Math.round(yAt(model.average)) + 0.5} />
            )}
            {model.unsure.map((flag, i) => flag && <path key={`u${i}`} className="c-unsure-dot" d={diamond(xAt(i), yAt(model.values[i]))} />)}
            <circle className="c-peak" cx={xAt(model.peakIndex)} cy={yAt(model.peak)} r={4} />
            {/* The figure is dropped when the scale's top tick already says it; a lower bound always keeps it */}
            {(peakUnsure || fmtFixed(model.peak, model.max < 1 ? 2 : 1) !== fmtFixed(model.max, model.max < 1 ? 2 : 1)) && (
              <text className="c-label c-label-accent" x={xAt(model.peakIndex) + (peakRight ? -8 : 8)} y={yAt(model.peak) - 8}
                textAnchor={peakRight ? 'end' : 'start'}>{peakUnsure ? '≥ ' : ''}{fmtFixed(model.peak, 2)}</text>
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
            {!blank && <circle className="c-hover-dot" cx={xAt(active)} cy={yAt(model.values[active])} r={3.5} />}
          </g>
        )}
      </svg>
      {empty && <p className="chart-empty">{emptyText}</p>}
      {bucket && <Tip W={W} x={xAt(active)} period={period} rows={rows} note={blank ? NO_DATA : null} />}
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
export function CacheChart({ buckets, windowSec, coveredFrom = null }) {
  const [ref, size] = useSize();
  const n = buckets.length;
  const W = size.w;
  const H = Math.max(size.h, 200);
  const plotW = Math.max(10, W - PAD.l - PAD.r);
  const gap = 20;
  const laneH = (H - PAD.t - PAD.b - gap) / 2;
  const [hover, onMove, onLeave, keyboard] = useHover(n, plotW);
  const unseen = uncoveredBuckets(buckets, windowSec, coveredFrom);

  const model = useMemo(() => {
    // The periods before the journal began carry no data: they are not a cold cache
    const seen = buckets.map((b, i) => (i < unseen ? {} : b));
    const reads = seen.map((b) => b.cache_read || 0);
    const writes = seen.map((b) => b.cache_write || 0);
    return { reads, writes, readMax: niceMax(Math.max(0, ...reads)), writeMax: niceMax(Math.max(0, ...writes)), spikes: writeSpikes(seen) };
  }, [buckets, unseen]);

  const readBase = PAD.t + laneH + 0.5;
  const writeTop = PAD.t + laneH + gap;
  const writeBase = writeTop + laneH + 0.5;
  const xAt = (i) => PAD.l + (n > 1 ? (i / (n - 1)) * plotW : plotW / 2);
  const readY = (v) => readBase - (v / model.readMax) * (laneH - 4);
  const writeY = (v) => writeBase - (v / model.writeMax) * (laneH - 4);
  const readPts = model.reads.map((v, i) => [xAt(i), readY(v)]).slice(unseen);
  const writePts = model.writes.map((v, i) => [xAt(i), writeY(v)]).slice(unseen);
  const readLine = smooth(readPts);
  const writeLine = smooth(writePts);
  const empty = !n || (!Math.max(0, ...model.reads) && !Math.max(0, ...model.writes));
  const labels = timeLabels(buckets, plotW);
  const active = hover !== null && buckets[hover] ? hover : null;
  const bucket = active !== null ? buckets[active] : null;
  const blank = active !== null && active < unseen;
  const spikes = [...model.spikes];
  const since = unseen > 0 ? fmtShortTime(coveredFrom) : null;
  const emptyText = since ? `С ${since} обращений к кэшу не было` : 'Обращений к кэшу за окно не было';
  const summary = empty ? emptyText
    : `Кэш за окно: прочитано ${fmtTokens(model.reads.reduce((s, v) => s + v, 0))}, записано ${fmtTokens(model.writes.reduce((s, v) => s + v, 0))}`
      + (spikes.length ? `, всплесков записи: ${spikes.length}` : '') + (since ? `. Данные только с ${since}` : '');
  const spike = bucket && model.spikes.has(active) ? 'запись выше обычной за окно' : null;
  const period = bucket && periodOf(bucket, windowSec, n);
  const rows = bucket && !blank ? [['прочитано', fmtTokens(bucket.cache_read)], ['записано', fmtTokens(bucket.cache_write)], ['без кэша', fmtTokens(bucket.input)]] : [];

  return (
    <div className="chart-frame chart-frame-cache" ref={ref}>
      <svg className="chart" width={W} height={H} aria-label={summary} {...keyboard}
        {...chartRole(n, active, bucket && (blank ? `${period}: ${NO_DATA}` : `${valueText(period, rows)}${spike ? `, ${spike}` : ''}`))}
        onPointerMove={onMove} onPointerLeave={onLeave}>
        {unseen > 0 && <Uncovered x0={PAD.l} x1={xAt(unseen)} y0={PAD.t} y1={writeBase} labelY={PAD.t + laneH / 2 + 4} />}
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
            {!blank && <circle className="c-hover-dot" cx={xAt(active)} cy={readY(model.reads[active])} r={3.5} />}
            {!blank && <circle className="c-hover-dot" cx={xAt(active)} cy={writeY(model.writes[active])} r={3.5} />}
          </g>
        )}
      </svg>
      {empty && <p className="chart-empty">{emptyText}</p>}
      {bucket && <Tip W={W} x={xAt(active)} period={period} rows={rows} note={blank ? NO_DATA : spike} />}
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
export function TurnTape({ trace, max, now, phases, className = '' }) {
  const p = phases || phasesOf(trace, now);
  const values = [p.wait, p.think, p.tools, p.out, p.other];
  const fill = max ? Math.max(1.5, (p.total / max) * 100) : 100;
  const label = PHASES.map((phase, i) => `${phase.label} ${fmtMs(values[i])}`).join(', ');
  return (
    <span className={`tape ${p.status === 'error' ? 'is-error' : ''} ${p.status === 'interrupted' ? 'is-interrupted' : ''} ${p.open ? 'is-open' : ''} ${className}`} role="img" aria-label={`Фазы хода: ${label}`}>
      <span className="tape-fill" style={{ width: `${Math.min(100, fill)}%` }}>
        {values.map((ms, i) => (ms > 0 ? <i key={PHASES[i].key} className={`ph-${i + 1}`} style={{ flexGrow: ms }} title={`${PHASES[i].label} ${fmtMs(ms)}`} /> : null))}
        {(p.open || p.status === 'error' || p.status === 'interrupted') && <i className="tape-cap" />}
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
