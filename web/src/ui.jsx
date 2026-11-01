import { createContext, useCallback, useContext, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Check, CircleCheck, CircleX, Copy, Info, TriangleAlert, X } from 'lucide-react';
import { spring } from './motion.jsx';

const ToastContext = createContext(() => {});

const TOAST_ICON = { success: CircleCheck, error: CircleX, warn: TriangleAlert, info: Info };

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const counter = useRef(0);
  const dismiss = useCallback((id) => setToasts((list) => list.filter((toast) => toast.id !== id)), []);
  const push = useCallback((toast) => {
    counter.current += 1;
    const id = counter.current;
    setToasts((list) => [...list.slice(-3), { tone: 'info', ...toast, id }]);
    setTimeout(() => dismiss(id), toast.duration || 4200);
  }, [dismiss]);
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        <AnimatePresence initial={false}>
          {toasts.map((toast) => {
            const Icon = TOAST_ICON[toast.tone] || Info;
            return (
              <motion.div
                key={toast.id}
                layout
                className={`toast tone-${toast.tone}`}
                initial={{ opacity: 0, x: 60, scale: 0.92 }}
                animate={{ opacity: 1, x: 0, scale: 1 }}
                exit={{ opacity: 0, x: 60, scale: 0.92, transition: { duration: 0.18 } }}
                transition={spring}
              >
                <Icon size={18} />
                <div>
                  <strong>{toast.title}</strong>
                  {toast.text && <p>{toast.text}</p>}
                </div>
                <button type="button" className="icon-button ghost" aria-label="Закрыть уведомление" onClick={() => dismiss(toast.id)}>
                  <X size={14} />
                </button>
                <motion.span
                  className="toast-timer"
                  initial={{ scaleX: 1 }} animate={{ scaleX: 0 }}
                  transition={{ duration: (toast.duration || 4200) / 1000, ease: 'linear' }}
                />
              </motion.div>
            );
          })}
        </AnimatePresence>
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  return useContext(ToastContext);
}

export function Badge({ tone = 'neutral', children, title }) {
  return <span className={`badge tone-${tone}`} title={title}>{children}</span>;
}

export function Button({ children, tone = 'default', icon: Icon, className = '', ...rest }) {
  return (
    <motion.button
      type="button"
      className={`button tone-${tone} ${className}`}
      whileHover={{ y: -1 }}
      whileTap={{ scale: 0.96 }}
      transition={spring}
      {...rest}
    >
      {Icon && <Icon size={16} aria-hidden="true" />}
      {children}
    </motion.button>
  );
}

export function CopyButton({ value, label = 'Копировать' }) {
  const [done, setDone] = useState(false);
  return (
    <motion.button
      type="button"
      className="icon-button"
      aria-label={label}
      title={label}
      whileTap={{ scale: 0.85 }}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setDone(true);
          setTimeout(() => setDone(false), 1400);
        } catch { /* clipboard may be blocked */ }
      }}
    >
      <AnimatePresence mode="wait" initial={false}>
        <motion.span
          key={done ? 'done' : 'copy'}
          initial={{ scale: 0.4, rotate: -40, opacity: 0 }}
          animate={{ scale: 1, rotate: 0, opacity: 1 }}
          exit={{ scale: 0.4, rotate: 40, opacity: 0 }}
          transition={{ duration: 0.16 }}
          className="icon-swap"
        >
          {done ? <Check size={15} /> : <Copy size={15} />}
        </motion.span>
      </AnimatePresence>
    </motion.button>
  );
}

export function Empty({ icon: Icon, title, text }) {
  return (
    <motion.div className="empty" initial={{ opacity: 0, scale: 0.96 }} animate={{ opacity: 1, scale: 1 }}>
      {Icon && (
        <motion.span className="empty-icon" animate={{ y: [0, -6, 0] }} transition={{ duration: 3, repeat: Infinity, ease: 'easeInOut' }}>
          <Icon size={26} />
        </motion.span>
      )}
      <strong>{title}</strong>
      {text && <p>{text}</p>}
    </motion.div>
  );
}

export function Toggle({ checked, onChange, label, id }) {
  return (
    <button
      type="button"
      role="switch"
      id={id}
      aria-checked={checked}
      aria-label={label}
      className={`toggle ${checked ? 'on' : ''}`}
      onClick={() => onChange(!checked)}
    >
      <motion.span className="toggle-knob" layout transition={spring} />
    </button>
  );
}
