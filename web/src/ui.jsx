import { createContext, useCallback, useContext, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Check, CircleAlert, CircleCheck, Copy, Info, X } from 'lucide-react';
import { ease } from './motion.jsx';

const ToastContext = createContext(() => {});

const TOAST_ICON = { success: CircleCheck, error: CircleAlert, warn: CircleAlert, info: Info };

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
              <motion.div key={toast.id} className={`toast tone-${toast.tone}`}
                initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, transition: { duration: 0.15 } }}
                transition={{ duration: 0.2, ease }}>
                <Icon size={16} strokeWidth={1.5} aria-hidden="true" />
                <div>
                  <strong>{toast.title}</strong>
                  {toast.text && <p>{toast.text}</p>}
                </div>
                <button type="button" className="icon-button ghost" aria-label="Закрыть уведомление" onClick={() => dismiss(toast.id)}>
                  <X size={16} strokeWidth={1.5} />
                </button>
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

/** Quiet status text: mono, muted, optionally toned. */
export function Tag({ tone = 'muted', children, title }) {
  return <span className={`tag tone-${tone}`} title={title}>{children}</span>;
}

export function Button({ children, tone = 'default', icon: Icon, className = '', ...rest }) {
  return (
    <button type="button" className={`button tone-${tone} ${className}`} {...rest}>
      {Icon && <Icon size={16} strokeWidth={1.5} aria-hidden="true" />}
      {children}
    </button>
  );
}

export function Kbd({ children }) {
  return <kbd className="keycap">{children}</kbd>;
}

export function CopyButton({ value, label = 'Копировать' }) {
  const [done, setDone] = useState(false);
  return (
    <button type="button" className="icon-button" aria-label={label} title={label}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setDone(true);
          setTimeout(() => setDone(false), 1400);
        } catch { /* clipboard may be blocked */ }
      }}>
      {done ? <Check size={16} strokeWidth={1.5} /> : <Copy size={16} strokeWidth={1.5} />}
    </button>
  );
}

export function Empty({ art, title, text, hint }) {
  return (
    <div className="empty">
      {art}
      <strong>{title}</strong>
      {text && <p>{text}</p>}
      {hint && <p className="empty-hint mono">{hint}</p>}
    </div>
  );
}

export function Toggle({ checked, onChange, label, id }) {
  return (
    <button type="button" role="switch" id={id} aria-checked={checked} aria-label={label}
      className={`toggle ${checked ? 'on' : ''}`} onClick={() => onChange(!checked)}>
      <span className="toggle-knob" />
    </button>
  );
}
