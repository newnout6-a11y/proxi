import { useId, useMemo, useState } from 'react';
import { motion } from 'motion/react';
import { ease, softSpring } from './motion.jsx';
import { isNum, fmtShortTime } from './format.js';

const W = 600;

function buildPath(points, height, max) {
  const usable = points.map((point, index) => ({ ...point, index })).filter((point) => isNum(point.v));
  if (!usable.length) return { line: '', area: '', coords: [] };
  const step = points.length > 1 ? W / (points.length - 1) : W;
  const coords = usable.map((point) => ({
    x: point.index * step,
    y: height - 6 - (max ? (point.v / max) * (height - 18) : 0),
    v: point.v, t: point.t, index: point.index,
  }));
  let line = `M${coords[0].x.toFixed(1)},${coords[0].y.toFixed(1)}`;
  for (let i = 1; i < coords.length; i += 1) {
    const prev = coords[i - 1];
    const cur = coords[i];
    const mid = (prev.x + cur.x) / 2;
    line += ` C${mid.toFixed(1)},${prev.y.toFixed(1)} ${mid.toFixed(1)},${cur.y.toFixed(1)} ${cur.x.toFixed(1)},${cur.y.toFixed(1)}`;
  }
  const area = `${line} L${coords[coords.length - 1].x.toFixed(1)},${height} L${coords[0].x.toFixed(1)},${height} Z`;
  return { line, area, coords };
}

/** Smooth animated area chart with a hover cursor. */
export function AreaChart({ points, height = 150, format = String, tone = 'accent', label }) {
  const id = useId().replace(/:/g, '');
  const [hover, setHover] = useState(null);
  const max = useMemo(() => Math.max(1, ...points.map((p) => (isNum(p.v) ? p.v : 0))) * 1.08, [points]);
  const { line, area, coords } = useMemo(() => buildPath(points, height, max), [points, height, max]);
  const step = points.length > 1 ? W / (points.length - 1) : W;
  const active = hover !== null ? coords.reduce((best, c) => (
    !best || Math.abs(c.index - hover) < Math.abs(best.index - hover) ? c : best), null) : null;

  return (
    <div className={`chart tone-${tone}`} role="img" aria-label={label}>
      <svg
        viewBox={`0 0 ${W} ${height}`}
        preserveAspectRatio="none"
        onMouseMove={(event) => {
          const box = event.currentTarget.getBoundingClientRect();
          setHover(Math.round(((event.clientX - box.left) / box.width) * W / step));
        }}
        onMouseLeave={() => setHover(null)}
      >
        <defs>
          <linearGradient id={`g${id}`} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="currentColor" stopOpacity="0.38" />
            <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
          </linearGradient>
        </defs>
        {[0.25, 0.5, 0.75].map((ratio) => (
          <line key={ratio} className="grid-line" x1="0" x2={W} y1={height * ratio} y2={height * ratio} />
        ))}
        {area && (
          <motion.path
            d={area} fill={`url(#g${id})`}
            initial={{ opacity: 0 }} animate={{ opacity: 1, d: area }} transition={{ duration: 0.8, ease }}
          />
        )}
        {line && (
          <motion.path
            d={line} fill="none" stroke="currentColor" strokeWidth="2.2" vectorEffect="non-scaling-stroke"
            initial={{ pathLength: 0 }} animate={{ pathLength: 1, d: line }} transition={{ duration: 1.1, ease }}
          />
        )}
        {active && (
          <g>
            <line className="cursor-line" x1={active.x} x2={active.x} y1="0" y2={height} vectorEffect="non-scaling-stroke" />
          </g>
        )}
      </svg>
      {active && (
        <motion.div
          className="chart-tip"
          initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }}
          style={{ left: `${Math.min(88, Math.max(4, (active.x / W) * 100))}%` }}
        >
          <b>{format(active.v)}</b><span>{fmtShortTime(active.t)}</span>
        </motion.div>
      )}
      {!line && <div className="chart-empty">Нет данных за окно</div>}
    </div>
  );
}

export function Sparkline({ values, height = 34 }) {
  const max = Math.max(1, ...values);
  const { line } = buildPath(values.map((v, index) => ({ v, t: index })), height, max);
  return (
    <svg className="sparkline" viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none" aria-hidden="true">
      {line && <motion.path d={line} fill="none" stroke="currentColor" strokeWidth="2" vectorEffect="non-scaling-stroke"
        animate={{ d: line }} transition={{ duration: 0.5, ease }} />}
    </svg>
  );
}

export function Bars({ items, format = String, empty = 'Пока пусто' }) {
  const max = Math.max(1, ...items.map((item) => item.value));
  if (!items.length) return <p className="muted small">{empty}</p>;
  return (
    <ul className="bars">
      {items.map((item, index) => (
        <li key={item.label}>
          <span className="bar-label" title={item.label}>{item.label}</span>
          <span className="bar-track">
            <motion.span
              className={`bar-fill tone-${item.tone || 'accent'}`}
              initial={{ scaleX: 0 }} animate={{ scaleX: item.value / max }}
              transition={{ ...softSpring, delay: index * 0.04 }}
            />
          </span>
          <span className="bar-value num">{format(item.value)}</span>
        </li>
      ))}
    </ul>
  );
}

export function Ring({ value, size = 132, label, caption }) {
  const radius = 52;
  const ratio = isNum(value) ? Math.max(0, Math.min(1, value)) : 0;
  return (
    <div className="ring" style={{ width: size, height: size }}>
      <svg viewBox="0 0 120 120" aria-hidden="true">
        <circle className="ring-track" cx="60" cy="60" r={radius} />
        <motion.circle
          className="ring-fill" cx="60" cy="60" r={radius} pathLength="1"
          initial={{ pathLength: 0 }} animate={{ pathLength: ratio }} transition={{ duration: 1.4, ease }}
        />
      </svg>
      <div className="ring-center">
        <strong className="num">{label}</strong>
        {caption && <span>{caption}</span>}
      </div>
    </div>
  );
}
