// The sample request a preview is made for: one of the recent requests the relay still holds, or a body the user pastes.
import { fmtShortTime } from '../format.js';
import { detectDialect, parseBody, parseHeaders } from './api.js';

export const DIALECTS = [
  { value: 'chat', label: 'Chat Completions' },
  { value: 'responses', label: 'Responses' },
  { value: 'anthropic', label: 'Anthropic Messages' },
];

export const emptySample = () => ({ mode: 'recent', requestId: '', text: '', dialect: '', headersText: '' });

export function recentLabel(item) {
  const parts = [fmtShortTime(item.ts), item.client || 'клиент?', item.model || 'модель?', item.purpose].filter((part) => part && part !== '—');
  return parts.join(' · ');
}

/**
 * {sample, error}: `sample` is what previewRequest takes, or null while nothing usable is chosen; `error` says why
 * a pasted body or its headers cannot be used (empty text is no error, just nothing yet).
 */
export function resolveSample(state, recent = []) {
  if (!state) return { sample: null, error: '' };
  if (state.mode === 'recent') {
    const item = recent.find((entry) => entry.request_id === state.requestId && entry.has_request);
    return { sample: item ? { kind: 'recent', request_id: item.request_id, label: recentLabel(item) } : null, error: '' };
  }
  if (!String(state.text || '').trim()) return { sample: null, error: '' };
  const body = parseBody(state.text);
  if (body.error) return { sample: null, error: body.error };
  const headers = parseHeaders(state.headersText);
  if (headers.error) return { sample: null, error: headers.error };
  const dialect = state.dialect || detectDialect(body.body);
  return { sample: { kind: 'paste', request: body.body, dialect, headers: headers.headers, label: `своё тело · ${dialect}` }, error: '' };
}

/** The newest recent request whose body the relay still holds: the sample to start from. */
export const newestSample = (recent) => recent.find((entry) => entry.has_request) || null;
