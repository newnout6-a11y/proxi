import { useCallback, useEffect, useState } from 'react';
import { AnimatePresence, motion, LayoutGroup } from 'motion/react';
import { Boxes, Gauge, LogOut, Moon, Route, ScrollText, Settings, Sun } from 'lucide-react';
import { api, clearKey, loadKey, saveKey } from './api.js';
import { ToastProvider, useToast } from './ui.jsx';
import { PulseDot, spring } from './motion.jsx';
import { useJournal } from './useJournal.js';
import Login from './Login.jsx';
import Overview from './views/Overview.jsx';
import Journal from './views/Journal.jsx';
import Turns from './views/Turns.jsx';
import Models from './views/Models.jsx';
import SettingsView from './views/Settings.jsx';

const NAV = [
  { id: 'overview', label: 'Обзор', icon: Gauge },
  { id: 'journal', label: 'Журнал', icon: ScrollText },
  { id: 'turns', label: 'Ходы', icon: Route },
  { id: 'models', label: 'Модели', icon: Boxes },
  { id: 'settings', label: 'Настройки', icon: Settings },
];

function initialTheme() {
  try {
    const saved = localStorage.getItem('relay.admin.theme');
    if (saved) return saved;
  } catch { /* ignore */ }
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function Shell({ apiKey, onLogout }) {
  const toast = useToast();
  const [view, setView] = useState(() => (NAV.some((n) => `#${n.id}` === location.hash) ? location.hash.slice(1) : 'overview'));
  const [theme, setTheme] = useState(initialTheme);
  const unauthorized = useCallback(() => {
    toast({ tone: 'error', title: 'Сессия админки закрыта', text: 'Ключ больше не принимается relay.' });
    onLogout(false);
  }, [onLogout, toast]);
  const journal = useJournal(apiKey, unauthorized);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('relay.admin.theme', theme); } catch { /* ignore */ }
  }, [theme]);
  useEffect(() => { history.replaceState(null, '', `#${view}`); }, [view]);
  useEffect(() => {
    const onKey = (event) => {
      if (event.target.closest('input, textarea, select') || event.metaKey || event.ctrlKey || event.altKey) return;
      const index = Number(event.key) - 1;
      if (index >= 0 && index < NAV.length) setView(NAV[index].id);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const errors = journal.events.filter((e) => e.level === 'error' && Date.now() / 1000 - e.ts < 600).length;
  const props = { apiKey, journal, onUnauthorized: unauthorized, go: setView };

  return (
    <div className="shell">
      <aside className="rail">
        <motion.div className="brand" initial={{ opacity: 0, x: -20 }} animate={{ opacity: 1, x: 0 }} transition={spring}>
          <span className="brand-mark">r</span>
          <div><strong>relay</strong><small>диспетчерская</small></div>
        </motion.div>
        <LayoutGroup>
          <nav aria-label="Разделы">
            {NAV.map((item, index) => {
              const Icon = item.icon;
              const active = view === item.id;
              return (
                <motion.button
                  key={item.id}
                  type="button"
                  className={`nav-item ${active ? 'active' : ''}`}
                  aria-current={active ? 'page' : undefined}
                  onClick={() => setView(item.id)}
                  initial={{ opacity: 0, x: -16 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ ...spring, delay: 0.05 * index }}
                  whileTap={{ scale: 0.97 }}
                >
                  {active && <motion.span layoutId="nav-pill" className="nav-pill" transition={spring} />}
                  <Icon size={18} aria-hidden="true" />
                  <span>{item.label}</span>
                  {item.id === 'journal' && errors > 0 && (
                    <motion.b className="nav-count" initial={{ scale: 0 }} animate={{ scale: 1 }} key={errors}
                      title="Ошибки за 10 минут">{errors}</motion.b>
                  )}
                  <kbd>{index + 1}</kbd>
                </motion.button>
              );
            })}
          </nav>
        </LayoutGroup>
        <div className="rail-foot">
          <div className={`stream-state state-${journal.status}`}>
            <PulseDot tone={journal.status === 'live' ? 'success' : 'warn'} live={journal.status === 'live'} />
            {journal.status === 'live' ? 'Поток журнала: live' : journal.status === 'reconnecting' ? 'Переподключение…' : 'Подключение…'}
          </div>
          <div className="rail-actions">
            <motion.button type="button" className="icon-button rail-btn" aria-label="Сменить тему"
              whileTap={{ rotate: 180, scale: 0.9 }} onClick={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))}>
              <AnimatePresence mode="wait" initial={false}>
                <motion.span key={theme} className="icon-swap" initial={{ y: -14, opacity: 0 }} animate={{ y: 0, opacity: 1 }} exit={{ y: 14, opacity: 0 }}>
                  {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
                </motion.span>
              </AnimatePresence>
            </motion.button>
            <button type="button" className="button tone-rail" onClick={() => onLogout(true)}>
              <LogOut size={15} /> Выйти
            </button>
          </div>
        </div>
      </aside>
      <main className="main">
        <AnimatePresence mode="wait">
          {view === 'overview' && <Overview key="overview" {...props} />}
          {view === 'journal' && <Journal key="journal" {...props} />}
          {view === 'turns' && <Turns key="turns" {...props} />}
          {view === 'models' && <Models key="models" {...props} />}
          {view === 'settings' && <SettingsView key="settings" {...props} />}
        </AnimatePresence>
      </main>
    </div>
  );
}

export default function App() {
  const [apiKey, setApiKey] = useState(loadKey);

  const login = useCallback((key, remember) => {
    saveKey(key, remember);
    setApiKey(key);
    api(key, 'session', { method: 'POST', body: { action: 'login' } }).catch(() => {});
  }, []);

  const logout = useCallback((announce) => {
    if (announce && apiKey) api(apiKey, 'session', { method: 'POST', body: { action: 'logout' } }).catch(() => {});
    clearKey();
    setApiKey('');
  }, [apiKey]);

  return (
    <ToastProvider>
      <AnimatePresence mode="wait">
        {apiKey ? (
          <motion.div key="shell" className="app-root" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            <Shell apiKey={apiKey} onLogout={logout} />
          </motion.div>
        ) : (
          <motion.div key="login" className="app-root" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0, scale: 1.03, filter: 'blur(10px)' }}>
            <Login onLogin={login} />
          </motion.div>
        )}
      </AnimatePresence>
    </ToastProvider>
  );
}
