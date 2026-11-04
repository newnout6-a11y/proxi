import { useEffect, useRef, useState } from 'react';
import { animate, motion, useReducedMotion } from 'motion/react';
import { isNum, fmtFixed } from './format.js';

export const ease = [0.22, 1, 0.36, 1];

/** The one entrance on view change: a short fade and an 8px rise. */
export function Page({ children, className = '' }) {
  const reduce = useReducedMotion();
  return (
    <motion.div className={`page ${className}`} initial={reduce ? false : { opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.3, ease }}>
      {children}
    </motion.div>
  );
}

/** A page on its own, or the same content inside another page (no second entrance). */
export function Section({ embedded, className = '', children }) {
  return embedded ? <div className={`stack ${className}`}>{children}</div> : <Page className={className}>{children}</Page>;
}

/** A figure that tweens from its previous value to the new one. */
export function AnimatedNumber({ value, format = (v) => fmtFixed(v, 2), className = '' }) {
  const reduce = useReducedMotion();
  const target = isNum(value) ? value : null;
  const [shown, setShown] = useState(target);
  const previous = useRef(target);
  useEffect(() => {
    if (target === null) { setShown(null); previous.current = null; return undefined; }
    const from = previous.current ?? target;
    previous.current = target;
    if (reduce || from === target) { setShown(target); return undefined; }
    const controls = animate(from, target, { duration: 0.6, ease, onUpdate: setShown });
    return () => controls.stop();
  }, [target, reduce]);
  return <span className={`num ${className}`}>{shown === null ? '—' : format(shown)}</span>;
}

export function Dot({ tone = 'muted', live = false }) {
  return <span className={`dot tone-${tone}${live ? ' live' : ''}`} aria-hidden="true" />;
}
