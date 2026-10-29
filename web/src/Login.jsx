import { useState } from 'react';
import { motion, AnimatePresence } from 'motion/react';
import { ArrowUpRight, Eye, EyeOff, KeyRound, Lock } from 'lucide-react';
import { api } from './api.js';
import { ease, spring } from './motion.jsx';

const word = 'relay';

export default function Login({ onLogin }) {
  const [key, setKey] = useState('');
  const [remember, setRemember] = useState(false);
  const [reveal, setReveal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [shake, setShake] = useState(0);

  async function submit(event) {
    event.preventDefault();
    if (!key.trim()) {
      setError('Введите relay key');
      setShake((n) => n + 1);
      return;
    }
    setBusy(true);
    setError('');
    try {
      await api(key.trim(), 'overview');
      onLogin(key.trim(), remember);
    } catch (failure) {
      setError(failure.status === 401 ? 'Неверный ключ' : failure.message);
      setShake((n) => n + 1);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-screen">
      <div className="orbs" aria-hidden="true">
        <motion.span className="orb orb-a" animate={{ x: [0, 80, -40, 0], y: [0, -60, 40, 0] }}
          transition={{ duration: 18, repeat: Infinity, ease: 'easeInOut' }} />
        <motion.span className="orb orb-b" animate={{ x: [0, -70, 50, 0], y: [0, 50, -30, 0] }}
          transition={{ duration: 22, repeat: Infinity, ease: 'easeInOut' }} />
        <div className="grid-overlay" />
      </div>
      <motion.form
        key={shake}
        className="login-card"
        onSubmit={submit}
        initial={shake ? { x: 0 } : { opacity: 0, y: 30, scale: 0.96 }}
        animate={shake ? { x: [0, -12, 10, -7, 5, 0] } : { opacity: 1, y: 0, scale: 1 }}
        transition={shake ? { duration: 0.42 } : { duration: 0.7, ease }}
      >
        <div className="login-brand" aria-label="relay">
          {word.split('').map((letter, index) => (
            <motion.span key={index} initial={{ y: 40, opacity: 0, rotate: -8 }} animate={{ y: 0, opacity: 1, rotate: 0 }}
              transition={{ ...spring, delay: 0.15 + index * 0.06 }}>{letter}</motion.span>
          ))}
          <motion.i initial={{ scale: 0 }} animate={{ scale: 1 }} transition={{ ...spring, delay: 0.55 }} />
        </div>
        <motion.h1 initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.4 }}>
          Диспетчерская
        </motion.h1>
        <motion.p className="muted" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.5 }}>
          Живой журнал каждого действия relay: запросы, ходы моделей, инструменты, кэш и кредиты.
        </motion.p>
        <label className="field" htmlFor="relay-key">
          <span>Relay key</span>
          <div className="input-wrap">
            <KeyRound size={16} aria-hidden="true" />
            <input
              id="relay-key"
              type={reveal ? 'text' : 'password'}
              autoComplete="current-password"
              value={key}
              onChange={(event) => setKey(event.target.value)}
              placeholder="relay-…"
              autoFocus
            />
            <button type="button" className="icon-button ghost" aria-label={reveal ? 'Скрыть ключ' : 'Показать ключ'}
              onClick={() => setReveal((v) => !v)}>
              {reveal ? <EyeOff size={15} /> : <Eye size={15} />}
            </button>
          </div>
        </label>
        <label className="check">
          <input type="checkbox" checked={remember} onChange={(event) => setRemember(event.target.checked)} />
          <span>Запомнить до закрытия вкладки</span>
        </label>
        <AnimatePresence>
          {error && (
            <motion.p className="form-error" role="alert" initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}>
              {error}
            </motion.p>
          )}
        </AnimatePresence>
        <motion.button type="submit" className="button tone-primary wide" disabled={busy}
          whileHover={{ y: -2 }} whileTap={{ scale: 0.97 }} transition={spring}>
          {busy ? <motion.span className="spinner" animate={{ rotate: 360 }} transition={{ duration: 0.8, repeat: Infinity, ease: 'linear' }} />
            : <Lock size={16} />}
          Войти
          <ArrowUpRight size={16} />
        </motion.button>
        <p className="hint">Ключ лежит в <code>relay_key.txt</code>. Он не попадает в URL и в сборку.</p>
      </motion.form>
    </div>
  );
}
