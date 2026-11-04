import { createContext, useContext } from 'react';

export const ThemeContext = createContext('dark');
export const useTheme = () => useContext(ThemeContext);

const KEY = 'relay.admin.theme';

export function initialTheme() {
  try {
    const saved = localStorage.getItem(KEY);
    if (saved === 'dark' || saved === 'light') return saved;
  } catch { /* storage may be disabled */ }
  return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

export function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
  try { localStorage.setItem(KEY, theme); } catch { /* ignore */ }
}
