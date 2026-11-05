import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { failureOf, previewPrompts } from './api.js';

/**
 * Where the focus goes once the next render is done: for an action that removes the control that had it (a delete, a
 * form that closes) or opens something to type in. The returned function takes `find`, which returns the element.
 */
export function useFocusLater() {
  const later = useRef(null);
  useLayoutEffect(() => {
    const find = later.current;
    if (!find) return;
    later.current = null;
    find()?.focus();
  });
  return useCallback((find) => { later.current = find; }, []);
}

/**
 * The answer to one preview request, asked again a moment after the request changes. A request that is
 * still in flight when the next one is due is cancelled, so a slow answer never replaces a newer one.
 */
export function usePreview(apiKey, body, { delay = 350, onUnauthorized } = {}) {
  const [state, setState] = useState({ data: null, error: null, loading: false });
  const unauthorized = useRef(onUnauthorized);
  unauthorized.current = onUnauthorized;
  useEffect(() => {
    if (!body) {
      setState({ data: null, error: null, loading: false });
      return undefined;
    }
    const controller = new AbortController();
    setState((previous) => (previous.loading ? previous : { ...previous, loading: true }));
    const timer = setTimeout(async () => {
      try {
        const data = await previewPrompts(apiKey, body, controller.signal);
        if (!controller.signal.aborted) setState({ data, error: null, loading: false });
      } catch (failure) {
        if (controller.signal.aborted || failure?.name === 'AbortError') return;
        if (failure.status === 401) unauthorized.current?.();
        else setState((previous) => ({ data: previous.data, error: failureOf(failure), loading: false }));
      }
    }, delay);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [apiKey, body, delay]);
  return state;
}

/** A line for a polite live region: the same sentence twice in a row is still announced. */
export function useAnnouncer() {
  const [message, setMessage] = useState('');
  const flip = useRef(false);
  const announce = useCallback((text) => {
    flip.current = !flip.current;
    setMessage(flip.current ? text : `${text}\u200b`);
  }, []);
  return [message, announce];
}
