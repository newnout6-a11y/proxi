import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  base: '/admin/',
  // The admin CSP allows only same-origin fonts and scripts: never inline assets as data: URLs.
  build: { sourcemap: false, assetsInlineLimit: 0 },
  server: { proxy: { '/admin/api': 'http://127.0.0.1:8787' } },
});
