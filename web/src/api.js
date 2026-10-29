const KEY_STORE = 'relay.admin.key.v1';

let memoryKey = '';

export function loadKey() {
  try {
    return memoryKey || sessionStorage.getItem(KEY_STORE) || '';
  } catch {
    return memoryKey;
  }
}

export function saveKey(key, remember) {
  memoryKey = key;
  try {
    if (remember) sessionStorage.setItem(KEY_STORE, key);
    else sessionStorage.removeItem(KEY_STORE);
  } catch { /* storage may be disabled */ }
}

export function clearKey() {
  memoryKey = '';
  try { sessionStorage.removeItem(KEY_STORE); } catch { /* ignore */ }
}

export class ApiError extends Error {
  constructor(status, message, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export async function api(key, path, { method = 'GET', body, signal } = {}) {
  const headers = { authorization: `Bearer ${key}` };
  if (body !== undefined) {
    headers['content-type'] = 'application/json';
    headers['x-admin-request'] = '1';
  }
  let response;
  try {
    response = await fetch(`/admin/api/${path}`, {
      method, headers, signal, cache: 'no-store',
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new ApiError(0, 'Relay недоступен', null);
  }
  let data = null;
  try { data = await response.json(); } catch { /* empty body */ }
  if (!response.ok) {
    throw new ApiError(response.status, data?.error?.message || response.statusText || 'Ошибка запроса', data);
  }
  return data;
}

/** Follow the journal over SSE with header auth (EventSource cannot send headers). */
export async function followJournal(key, since, { signal, onEvent, onHello, onReset }) {
  const response = await fetch(`/admin/api/events/stream?since=${since}`, {
    headers: { authorization: `Bearer ${key}` }, signal, cache: 'no-store',
  });
  if (!response.ok || !response.body) throw new ApiError(response.status, 'Поток журнала недоступен');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });
    let index;
    while ((index = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      let name = 'message';
      let data = '';
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) name = line.slice(6).trim();
        else if (line.startsWith('data:')) data += line.slice(5).trim();
      }
      if (!data) continue;
      let payload;
      try { payload = JSON.parse(data); } catch { continue; }
      if (name === 'journal') onEvent(payload);
      else if (name === 'hello') onHello?.(payload);
      else if (name === 'reset') onReset?.(payload);
    }
  }
}

export function sleep(ms, signal) {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
  });
}
