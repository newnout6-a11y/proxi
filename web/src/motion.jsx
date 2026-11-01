import { useEffect, useState } from 'react';
import { motion, useSpring, useMotionValueEvent } from 'motion/react';
import { isNum, fmtNum } from './format.js';

export const ease = [0.22, 1, 0.36, 1];
export const spring = { type: 'spring', stiffness: 420, damping: 34 };
export const softSpring = { type: 'spring', stiffness: 170, damping: 22 };

export const pageVariants = {
  initial: { opacity: 0, y: 18, filter: 'blur(8px)' },
  animate: {
    opacity: 1, y: 0, filter: 'blur(0px)',
    transition: { duration: 0.45, ease, staggerChildren: 0.055, delayChildren: 0.04 },
  },
  exit: { opacity: 0, y: -12, filter: 'blur(6px)', transition: { duration: 0.18, ease } },
};

export const itemVariants = {
  initial: { opacity: 0, y: 18, scale: 0.985 },
  animate: { opacity: 1, y: 0, scale: 1, transition: softSpring },
};

export function Page({ children, className = '' }) {
  return (
    <motion.div className={`page ${className}`} variants={pageVariants} initial="initial" animate="animate" exit="exit">
      {children}
    </motion.div>
  );
}

export function Reveal({ as = 'div', children, className = '', ...rest }) {
  const Component = motion[as];
  return <Component className={className} variants={itemVariants} {...rest}>{children}</Component>;
}

/** Number that springs from its previous value to the new one. */
export function AnimatedNumber({ value, format = (v) => fmtNum(v, 2), className = '' }) {
  const target = isNum(value) ? value : 0;
  const motionValue = useSpring(0, { stiffness: 70, damping: 18, mass: 0.9 });
  const [shown, setShown] = useState(0);
  useEffect(() => { motionValue.set(target); }, [target, motionValue]);
  useMotionValueEvent(motionValue, 'change', (latest) => setShown(latest));
  return <span className={`num ${className}`}>{isNum(value) ? format(shown) : '—'}</span>;
}

/** Briefly highlights its content whenever ``signal`` changes. */
export function Flash({ signal, children, className = '' }) {
  return (
    <motion.span
      key={signal}
      className={className}
      initial={{ backgroundColor: 'var(--accent-soft)' }}
      animate={{ backgroundColor: 'rgba(0,0,0,0)' }}
      transition={{ duration: 1.2, ease }}
    >
      {children}
    </motion.span>
  );
}

export function PulseDot({ tone = 'success', live = true }) {
  return (
    <span className={`pulse-dot tone-${tone}`} aria-hidden="true">
      {live && (
        <motion.span
          className="pulse-ring"
          initial={{ scale: 0.6, opacity: 0.7 }}
          animate={{ scale: 2.4, opacity: 0 }}
          transition={{ duration: 1.6, repeat: Infinity, ease: 'easeOut' }}
        />
      )}
    </span>
  );
}
