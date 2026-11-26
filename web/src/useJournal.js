import { useCallback, useEffect, useRef, useState } from 'react';
import { api, followJournal, sleep } from './api.js';

const CAP = 3000;

export function mergeEvents(previous, items) {
  const last = previous.length ? previous[previous.length - 1].seq : 0;
  const fresh = items.filter((event) => event.seq > last);
  if (!fresh.length) return previous;
  const next = previous.concat(fresh);
  return next.length > CAP ? next.slice(next.length - CAP) : next;
}

/**
 * Journal event buffer shared by the hook and unit tests: tracks a monotonic `resetEpoch` so an SSE `reset` + fresh
 * `seq=1` event arriving before POST `events/clear` resolves is never wiped by a late client-side `reset(epoch)`.
 */
export function createJournalBuffer(onUpdate) {
  let events = [];
  let queue = [];
  let timer = null;
  let lastSeq = 0;
  let resetEpoch = 0;
  let serverEpoch;

  const flush = () => {
    timer = null;
    const items = queue;
    queue = [];
    if (!items.length) return;
    events = mergeEvents(events, items);
    onUpdate(events);
  };

  function reset(expectedEpoch) {
    if (expectedEpoch !== undefined && resetEpoch !== expectedEpoch) return false;
    resetEpoch += 1;
    clearTimeout(timer);
    timer = null;
    lastSeq = 0;
    queue = [];
    if (events.length) {
      events = [];
      onUpdate(events);
    }
    return true;
  }

  return {
    get events() { return events; },
    get lastSeq() { return lastSeq; },
    get resetEpoch() { return resetEpoch; },
    get serverEpoch() { return serverEpoch; },
    loadInitial(data, serverLastSeq = 0, startEpoch = resetEpoch, epoch) {
      if (resetEpoch !== startEpoch) return false;
      events = Array.isArray(data) ? data : [];
      lastSeq = events.length ? Math.max(...events.map((e) => e.seq || 0)) : (serverLastSeq || 0);
      serverEpoch = Number.isSafeInteger(epoch) ? epoch : undefined;
      onUpdate(events);
      return true;
    },
    onHello(epoch) {
      // If the stream drops before an announced reset arrives, resume with the old cursor again.
      if (serverEpoch === undefined && Number.isSafeInteger(epoch)) serverEpoch = epoch;
    },
    resetFromServer(epoch) {
      serverEpoch = Number.isSafeInteger(epoch) ? epoch : undefined;
      reset();
    },
    push(event) {
      lastSeq = Math.max(lastSeq, event.seq);
      queue.push(event);
      if (!timer) timer = setTimeout(flush, 70);
    },
    flushNow() {
      clearTimeout(timer);
      flush();
    },
    reset,
    dispose() {
      clearTimeout(timer);
      timer = null;
    },
  };
}

/** Live journal: initial page, then an auto-reconnecting SSE follower. The pause belongs to the list of events (journalPause.js):
    the stream itself always flows, so turns, counts and the error badge stay live. */
export function useJournal(key, onUnauthorized) {
  const [events, setEvents] = useState([]);
  const [status, setStatus] = useState('connecting');
  const bufferRef = useRef(null);
  if (!bufferRef.current) bufferRef.current = createJournalBuffer(setEvents);
  const unauthorized = useRef(onUnauthorized);
  unauthorized.current = onUnauthorized;

  useEffect(() => {
    const buffer = bufferRef.current;
    buffer.resetFromServer();
    const controller = new AbortController();
    const { signal } = controller;
    let stopped = false;
    (async () => {
      try {
        const startEpoch = buffer.resetEpoch;
        const first = await api(key, 'events?limit=800', { signal });
        buffer.loadInitial(first.data, first.last_seq, startEpoch, first.reset_epoch);
      } catch (error) {
        if (error.status === 401) { unauthorized.current?.(); return; }
      }
      let delay = 1000;
      while (!stopped) {
        try {
          await followJournal(key, buffer.lastSeq, {
            signal,
            resetEpoch: buffer.serverEpoch,
            onHello: (event) => { buffer.onHello(event.reset_epoch); setStatus('live'); delay = 1000; },
            onEvent: (event) => buffer.push(event),
            onReset: (event) => buffer.resetFromServer(event.reset_epoch),
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
      buffer.dispose();
    };
  }, [key]);

  const reset = useCallback((expectedEpoch) => bufferRef.current.reset(expectedEpoch), []);
  const epoch = useCallback(() => bufferRef.current.resetEpoch, []);

  return { events, status, reset, epoch };
}

/** Run ``task`` immediately and on an interval, but only while the tab is visible. */
export function usePolling(task, interval, deps) {
  const saved = useRef(task);
  saved.current = task;
  useEffect(() => {
    let cancelled = false;
    let handle = null;
    // One chain at a time: a tab shown again while a poll was in flight used to start a second chain beside the first,
    // which re-armed itself too, so every Alt-Tab during a slow poll added a polling loop for good.
    let running = false;
    const tick = async () => {
      if (cancelled || running) return;
      running = true;
      try {
        if (document.visibilityState === 'visible') {
          try { await saved.current(); } catch { /* surfaced by the task */ }
        }
      } finally {
        running = false;
      }
      if (!cancelled) handle = setTimeout(tick, interval);
    };
    tick();
    const onVisible = () => {
      if (document.visibilityState === 'visible' && !running) {
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
