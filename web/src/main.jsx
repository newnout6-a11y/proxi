import React from 'react';
import { createRoot } from 'react-dom/client';
import '@fontsource-variable/manrope';
import '@fontsource/ibm-plex-mono/latin-400.css';
import '@fontsource/ibm-plex-mono/cyrillic-400.css';
import './tokens.css';
import './styles.css';
import './stage/stage.css';
import { applyTheme, initialTheme } from './theme.js';
import App from './App.jsx';

applyTheme(initialTheme());
createRoot(document.getElementById('root')).render(<App />);
