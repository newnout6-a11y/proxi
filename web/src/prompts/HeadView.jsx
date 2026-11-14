import { useMemo } from 'react';
import { CopyButton } from '../ui.jsx';
import { fmtInt } from '../format.js';
import { segmentHead, sourceLabel, summarizeKinds } from './parts.js';

/**
 * The composed head with every piece tinted by where it came from. The tint is never the only cue: the legend
 * names each kind with its size and the parts list below gives every piece in words.
 */
export default function HeadView({ head, parts, label = 'Итоговый head', tall = false }) {
  const segments = useMemo(() => segmentHead(head, parts), [head, parts]);
  const kinds = useMemo(() => summarizeKinds(segments), [segments]);
  if (!head) return <p className="muted small p-empty-line">Head пуст: модель получит запрос без system</p>;
  return (
    <div className="head-view">
      <div className="head-bar">
        <ul className="src-legend" aria-label="Источники частей head">
          {kinds.map((entry) => (
            <li key={entry.kind}><i className={`swatch src-${entry.kind}`} aria-hidden="true" />{entry.label}<b className="num">{fmtInt(entry.chars)}</b></li>
          ))}
        </ul>
        <CopyButton value={head} label="Копировать head" />
      </div>
      <div className={`head-scroll${tall ? ' tall' : ''}`} tabIndex={0} role="region" aria-label={label}>
        <pre className="head-text">
          {segments.map((segment, index) => (
            <span key={index} className={`src src-${segment.kind}`} data-source={segment.source}
              title={`${sourceLabel(segment.source)} · ${fmtInt(segment.chars)} симв.${segment.sha8 ? ` · ${segment.sha8}` : ''}`}>{segment.text}</span>
          ))}
        </pre>
      </div>
      <details className="head-parts">
        <summary>Части head: {fmtInt(segments.length)}</summary>
        <ol>
          {segments.map((segment, index) => (
            <li key={index}>
              <i className={`swatch src-${segment.kind}`} aria-hidden="true" />
              <span>{sourceLabel(segment.source)}</span>
              <span className="mono muted">{fmtInt(segment.chars)} симв.{segment.sha8 ? ` · ${segment.sha8}` : ''}</span>
            </li>
          ))}
        </ol>
      </details>
    </div>
  );
}
