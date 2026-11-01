import { readFile, writeFile } from 'node:fs/promises';

const tokens = JSON.parse(await readFile(new URL('../src/tokens.json', import.meta.url), 'utf8'));
const block = (selector, values) => `${selector} {\n${Object.entries(values).map(([key, value]) => `  --${key}: ${value};`).join('\n')}\n}\n`;
await writeFile(new URL('../src/tokens.css', import.meta.url),
  '/* Generated from tokens.json by npm run tokens. */\n' + block(':root', tokens.light) + block('[data-theme="dark"]', tokens.dark));
