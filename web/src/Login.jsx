import { useState } from 'react';
import { Eye, EyeOff, KeyRound } from 'lucide-react';
import { api } from './api.js';
import { LoginHero } from './stage/LoginHero.jsx';

function Mark() {
  return (
    <svg className="brand-mark" width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
      <rect x="2" y="3" width="16" height="3" rx="1" className="m-ink" />
      <rect x="2" y="8.5" width="10" height="3" rx="1" className="m-ink" />
      <rect x="2" y="14" width="13" height="3" rx="1" className="m-accent" />
    </svg>
  );
}

export default function Login({ onLogin }) {
  const [key, setKey] = useState('');
  const [remember, setRemember] = useState(false);
  const [reveal, setReveal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function submit(event) {
    event.preventDefault();
    if (!key.trim()) {
      setError('Введите relay key');
      return;
    }
    setBusy(true);
    setError('');
    try {
      await api(key.trim(), 'overview');
      onLogin(key.trim(), remember);
    } catch (failure) {
      setError(failure.status === 401 ? 'Неверный ключ' : failure.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-screen">
      <form className="login-form" onSubmit={submit} noValidate>
        <div className="brand"><Mark /><strong>Proxi</strong></div>
        <div className="login-copy">
          <h1>Весь relay перед глазами</h1>
          <p>Баланс аккаунтов, расход кредитов и каждый ход модели — от запроса IDE до ответа Retool</p>
        </div>
        <label className="field" htmlFor="relay-key">
          <span>Relay key</span>
          <span className="input-wrap">
            <KeyRound size={16} strokeWidth={1.5} aria-hidden="true" />
            <input id="relay-key" type={reveal ? 'text' : 'password'} autoComplete="current-password" value={key}
              onChange={(event) => setKey(event.target.value)} placeholder="relay-…" autoFocus spellCheck={false} />
            <button type="button" className="icon-button ghost" aria-label={reveal ? 'Скрыть ключ' : 'Показать ключ'}
              onClick={() => setReveal((v) => !v)}>
              {reveal ? <EyeOff size={16} strokeWidth={1.5} /> : <Eye size={16} strokeWidth={1.5} />}
            </button>
          </span>
        </label>
        <label className="check">
          <input type="checkbox" checked={remember} onChange={(event) => setRemember(event.target.checked)} />
          <span>Запомнить до закрытия вкладки</span>
        </label>
        {error && <p className="form-error" role="alert">{error}</p>}
        <button type="submit" className="button tone-primary login-submit" disabled={busy}>
          {busy ? 'Проверка' : 'Войти'}
        </button>
        <p className="hint">Ключ лежит в <code>relay_key.txt</code> рядом с config.json. Он не попадает в URL и в сборку</p>
      </form>
      <div className="login-hero">
        <LoginHero />
      </div>
    </div>
  );
}
