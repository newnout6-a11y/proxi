import { useMemo, useState } from 'react';
import { fmtInt } from '../format.js';
import { diffStats, foldDiff } from './diff.js';

const MARK = { add: '+', del: '−', same: '' };
const SAID = { add: 'добавлено: ', del: 'убрано: ', same: '' };

/** A unified line diff; unchanged stretches fold away and open on demand. */
export default function DiffView({ rows, label = 'Различия', emptyText = 'Различий нет', unchangedText }) {
  const [open, setOpen] = useState(() => new Set());
  const stats = useMemo(() => diffStats(rows), [rows]);
  const folded = useMemo(() => foldDiff(rows, 2), [rows]);
  if (!rows.length) return <p className="muted small p-empty-line">{emptyText}</p>;
  if (!stats.changed) return <p className="muted small p-empty-line">{unchangedText || emptyText}</p>;
  const line = (row, key) => (
    <li key={key} className={`diff-row diff-${row.type}`}>
      <span className="diff-mark" aria-hidden="true">{MARK[row.type]}</span>
      <span className="p-sr">{SAID[row.type]}</span>
      <span className="diff-text">{row.text === '' ? '\u00a0' : row.text}</span>
    </li>
  );
  return (
    <div className="diff-view">
      <p className="diff-stats mono">
        <span className="pos-text">+{fmtInt(stats.added)}</span> <span className="neg-text">−{fmtInt(stats.removed)}</span>
      </p>
      <div className="diff-scroll" tabIndex={0} role="region" aria-label={label}>
        <ol className="diff">
          {folded.map((row, index) => {
            if (row.type !== 'fold') return line(row, index);
            if (open.has(index)) return row.rows.map((inner, innerIndex) => line(inner, `${index}.${innerIndex}`));
            return (
              <li key={index} className="diff-fold">
                <button type="button" className="link-button" onClick={() => setOpen((previous) => new Set(previous).add(index))}>
                  Показать неизменённые строки: {fmtInt(row.rows.length)}
                </button>
              </li>
            );
          })}
        </ol>
      </div>
    </div>
  );
}
