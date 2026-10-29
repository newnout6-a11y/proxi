import { useCallback, useEffect, useRef, useState } from 'react';
import { api, followJournal, sleep } from './api.js';

const CAP = 3000;

function merge(previous, items) {
  const last = previous.length ? previous[previous.length - 1].seq : 0;
  const fresh = items.filter((event) => event.seq > last);
  if (!fresh.length) return previous;
  const next = previous.concat(fresh);
  return next.length > CAP ? next.slice(next.length - CAP) : next;
}

/** Live journal: initial page, then an auto-reconnecting SSE follower with pause buffering. */
export function useJournal(key, onUnauthorized) {
  const [events, setEvents] = useState([]);
  const [status, setStatus] = useState('connecting');
  const [paused, setPausedState] = useState(false);
  const [pending, setPending] = useState(0);
  const pausedRef = useRef(false);
  const buffer = useRef([]);
  const queue = useRef([]);
  const timer = useRef(null);
  const lastSeq = useRef(0);
  const unauthorized = useRef(onUnauthorized);
  unauthorized.current = onUnauthorized;

  const flush = useCallback(() => {
    timer.current = null;
    const items = queue.current;
    queue.current = [];
    if (!items.length) return;
    if (pausedRef.current) {
      buffer.current.push(...items);
      setPending(buffer.current.length);
      return;
    }
    setEvents((previous) => merge(previous, items));
  }, []);

  const push = useCallback((event) => {
    lastSeq.current = Math.max(lastSeq.current, event.seq);
    queue.current.push(event);
    if (!timer.current) timer.current = setTimeout(flush, 70);
  }, [flush]);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    let stopped = false;
    (async () => {
      try {
        const first = await api(key, 'events?limit=800', { signal });
        setEvents(first.data);
        lastSeq.current = first.last_seq;
      } catch (error) {
        if (error.status === 401) { unauthorized.current?.(); return; }
      }
      let delay = 1000;
      while (!stopped) {
        try {
          await followJournal(key, lastSeq.current, {
            signal,
            onHello: () => { setStatus('live'); delay = 1000; },
            onEvent: push,
            onReset: () => {
              lastSeq.current = 0;
              queue.current = [];
              buffer.current = [];
              setPending(0);
              setEvents([]);
            },
          });
        } catch (error) {
          if (stopped) return;
          if (error.status === 401) { unauthorized.current?.(); return; }
        }
        if (stopped) return;
        setStatus('reconnecting');
        await sleep(delay, signal);
        delay = Math.min(delay * 2, 15000);
      }
    })();
    return () => {
      stopped = true;
      controller.abort();
      clearTimeout(timer.current);
      timer.current = null;
    };
  }, [key, push]);

  const setPaused = useCallback((value) => {
    pausedRef.current = value;
    setPausedState(value);
    if (!value && buffer.current.length) {
      const items = buffer.current;
      buffer.current = [];
      setPending(0);
      setEvents((previous) => merge(previous, items));
    }
  }, []);

  const reset = useCallback(() => {
    lastSeq.current = 0;
    buffer.current = [];
    queue.current = [];
    setPending(0);
    setEvents([]);
  }, []);

  return { events, status, paused, pending, setPaused, reset };
}

/** Run ``task`` immediately and on an interval, but only while the tab is visible. */
export function usePolling(task, interval, deps) {
  const saved = useRef(task);
  saved.current = task;
  useEffect(() => {
    let cancelled = false;
    let handle = null;
    const tick = async () => {
      if (cancelled) return;
      if (document.visibilityState === 'visible') {
        try { await saved.current(); } catch { /* surfaced by the task */ }
      }
      if (!cancelled) handle = setTimeout(tick, interval);
    };
    tick();
    const onVisible = () => {
      if (document.visibilityState === 'visible') {
        clearTimeout(handle);
        tick();
      }
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      cancelled = true;
      clearTimeout(handle);
      document.removeEventListener('visibilitychange', onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interval, ...deps]);
}
