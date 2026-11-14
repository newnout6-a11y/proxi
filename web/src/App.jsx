import { useCallback, useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Activity as ActivityIcon, Gauge, LogOut, Moon, ScrollText, Settings, Sun, Users } from 'lucide-react';
import { api, clearKey, loadKey, saveKey } from './api.js';
import { ToastProvider, useToast, Kbd } from './ui.jsx';
import { Dot, ease } from './motion.jsx';
import { ThemeContext, applyTheme, initialTheme } from './theme.js';
import { useJournal, usePolling } from './useJournal.js';
import Login from './Login.jsx';
import Overview from './views/Overview.jsx';
import Accounts from './views/Accounts.jsx';
import Activity from './views/Activity.jsx';
import SettingsView from './views/Settings.jsx';
import PromptsView from './views/Prompts.jsx';

const NAV = [
  { id: 'overview', label: 'Сводка', icon: Gauge, group: 'Наблюдение' },
  { id: 'activity', label: 'Активность', icon: ActivityIcon, group: 'Наблюдение' },
  { id: 'accounts', label: 'Аккаунты', icon: Users, group: 'Управление' },
  { id: 'settings', label: 'Настройки', icon: Settings, group: 'Управление' },
  { id: 'prompts', label: 'Промпты', icon: ScrollText, group: 'Управление' },
];
const GROUPS = ['Наблюдение', 'Управление'];

function viewFromHash() {
  const id = location.hash.slice(1).split('/')[0];
  return NAV.some((n) => n.id === id) ? id : 'overview';
}

function Mark() {
  return (
    <svg className="brand-mark" width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
      <rect x="2" y="3" width="16" height="3" rx="1" className="m-ink" />
      <rect x="2" y="8.5" width="10" height="3" rx="1" className="m-ink" />
      <rect x="2" y="14" width="13" height="3" rx="1" className="m-accent" />
    </svg>
  );
}

function Shell({ apiKey, onLogout, theme, setTheme }) {
  const toast = useToast();
  const [view, setView] = useState(viewFromHash);
  const [overview, setOverview] = useState(null);
  const [overviewError, setOverviewError] = useState('');
  const viewRef = useRef(view);
  viewRef.current = view;
  // Settings keeps its draft in its own state and reports how many edits are unsaved: leaving drops them.
  const unsaved = useRef(0);
  const confirmLeave = useCallback(() => unsaved.current === 0
    || window.confirm(`Есть несохранённые настройки (${unsaved.current}). Уйти без сохранения?`), []);
  const go = useCallback((next) => {
    if (next === viewRef.current || confirmLeave()) setView(next);
  }, [confirmLeave]);
  const unauthorized = useCallback(() => {
    toast({ tone: 'error', title: 'Сессия админки закрыта', text: 'Ключ больше не принимается relay' });
    onLogout(false);
  }, [onLogout, toast]);
  const journal = useJournal(apiKey, unauthorized);

  const loadOverview = useCallback(async () => {
    try {
      setOverview(await api(apiKey, 'overview'));
      setOverviewError('');
    } catch (failure) {
      if (failure.status === 401) unauthorized();
      else setOverviewError(failure.message);
    }
  }, [apiKey, unauthorized]);
  usePolling(loadOverview, 8000, [apiKey]);

  useEffect(() => {
    // Activity keeps its own sub-tab in the hash (#activity/events).
    if (!location.hash.startsWith(`#${view}`)) history.replaceState(null, '', `#${view}`);
  }, [view]);
  useEffect(() => {
    const onKey = (event) => {
      if (event.target.closest('input, textarea, select') || event.metaKey || event.ctrlKey || event.altKey) return;
      const index = Number(event.key) - 1;
      if (index >= 0 && index < NAV.length) go(NAV[index].id);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [go]);
  useEffect(() => {
    const warn = (event) => {
      if (unsaved.current) { event.preventDefault(); event.returnValue = ''; }
    };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, []);

  const errors = journal.events.filter((e) => e.level === 'error' && Date.now() / 1000 - e.ts < 600).length;
  const props = { apiKey, journal, overview, overviewError, refreshOverview: loadOverview, onUnauthorized: unauthorized, go };
  const streamLabel = journal.status === 'live' ? 'Поток журнала: live' : journal.status === 'reconnecting' ? 'Переподключение' : 'Подключение';

  return (
    <div className="shell">
      <aside className="rail">
        <div className="brand"><Mark /><strong>Proxi</strong><span className="brand-caption">Retool relay</span></div>
        <nav aria-label="Разделы">
          {GROUPS.map((group) => (
            <div className="nav-group" key={group}>
              <p className="nav-label">{group}</p>
              {NAV.map((item, index) => {
                if (item.group !== group) return null;
                const Icon = item.icon;
                const active = view === item.id;
                return (
                  <button key={item.id} type="button" className={`nav-item ${active ? 'active' : ''}`}
                    aria-current={active ? 'page' : undefined} onClick={() => go(item.id)}>
                    <Icon size={16} strokeWidth={1.5} aria-hidden="true" />
                    <span className="nav-text">{item.label}</span>
                    {item.id === 'activity' && errors > 0 && <b className="nav-count num" title="Ошибки за 10 минут">{errors}</b>}
                    <Kbd>{index + 1}</Kbd>
                  </button>
                );
              })}
            </div>
          ))}
        </nav>
        <div className="rail-foot">
          {/* The dot alone carries the state; the words stay for the tooltip and screen readers */}
          <div className="stream-state" role="status" title={streamLabel} aria-label={streamLabel}>
            <Dot tone={journal.status === 'live' ? 'pos' : 'warn'} live={journal.status === 'live'} />
            <span>{journal.status === 'live' ? 'Журнал подключён' : journal.status === 'reconnecting' ? 'Переподключение' : 'Подключаем журнал'}</span>
          </div>
          <div className="rail-actions">
            <button type="button" className="icon-button" aria-label="Сменить тему" onClick={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))}>
              {theme === 'dark' ? <Sun size={16} strokeWidth={1.5} /> : <Moon size={16} strokeWidth={1.5} />}
            </button>
            <button type="button" className="button" onClick={() => { if (confirmLeave()) onLogout(true); }}>
              <LogOut size={16} strokeWidth={1.5} aria-hidden="true" /> Выйти
            </button>
          </div>
        </div>
      </aside>
      <main className="main">
        {view === 'overview' && <Overview key="overview" {...props} />}
        {view === 'activity' && <Activity key="activity" {...props} />}
        {view === 'accounts' && <Accounts key="accounts" {...props} />}
        {view === 'settings' && <SettingsView key="settings" {...props} unsaved={unsaved} />}
        {view === 'prompts' && <PromptsView key="prompts" {...props} unsaved={unsaved} />}
      </main>
    </div>
  );
}

export default function App() {
  const [apiKey, setApiKey] = useState(loadKey);
  const [theme, setTheme] = useState(initialTheme);
  useEffect(() => { applyTheme(theme); }, [theme]);

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
    <ThemeContext.Provider value={theme}>
      <ToastProvider>
        <AnimatePresence mode="wait">
          <motion.div key={apiKey ? 'shell' : 'login'} className="app-root" initial={{ opacity: 0 }} animate={{ opacity: 1 }}
            exit={{ opacity: 0 }} transition={{ duration: 0.2, ease }}>
            {apiKey ? <Shell apiKey={apiKey} onLogout={logout} theme={theme} setTheme={setTheme} /> : <Login onLogin={login} />}
          </motion.div>
        </AnimatePresence>
      </ToastProvider>
    </ThemeContext.Provider>
  );
}
