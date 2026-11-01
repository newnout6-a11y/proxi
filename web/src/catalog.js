import {
  Network, Sparkles, Zap, Wrench, Terminal, Layers, KeyRound, Database, ShieldCheck, Server, CircleDot,
} from 'lucide-react';

export const CATEGORIES = {
  http: { label: 'HTTP', icon: Network },
  turn: { label: 'Ход', icon: Sparkles },
  upstream: { label: 'Upstream', icon: Zap },
  tool: { label: 'Инструмент', icon: Wrench },
  exec: { label: 'Команда', icon: Terminal },
  session: { label: 'Сессия', icon: Layers },
  auth: { label: 'Авторизация', icon: KeyRound },
  cache: { label: 'Кэш', icon: Database },
  admin: { label: 'Админ', icon: ShieldCheck },
  relay: { label: 'Relay', icon: Server },
};

export const LEVELS = {
  debug: { label: 'debug' },
  info: { label: 'info' },
  success: { label: 'ok' },
  warn: { label: 'warn' },
  error: { label: 'error' },
};

export function category(name) {
  return CATEGORIES[name] || { label: name || 'прочее', icon: CircleDot };
}

export const TURN_STATUS = {
  running: 'идёт',
  stop: 'готово',
  tool_calls: 'инструменты',
  cancelled: 'отменён',
  timeout: 'таймаут',
  error: 'ошибка',
};
