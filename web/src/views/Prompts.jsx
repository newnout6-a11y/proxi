import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Copy, RefreshCw, RotateCcw, Save, TriangleAlert } from 'lucide-react';
import { Button, Tag, useToast } from '../ui.jsx';
import { Page, ease } from '../motion.jsx';
import { usePolling } from '../useJournal.js';
import { fmtInt } from '../format.js';
import { failureLead, failureOf, loadPrompts, loadRecent, savePrompts } from '../prompts/api.js';
import { ErrorContext, Notice } from '../prompts/controls.jsx';
import { addProfile, blankDocument, changedPaths, equalJson, errorHits, insertAt, locate, newRule, normalizePath, pathLabel, profilesOf, rulesOf, starterProfile } from '../prompts/draft.js';
import LegacyStart from '../prompts/LegacyStart.jsx';
import PinsTab from '../prompts/PinsTab.jsx';
import PreviewTab from '../prompts/PreviewTab.jsx';
import ProfilesTab from '../prompts/ProfilesTab.jsx';
import RulesTab from '../prompts/RulesTab.jsx';
import { emptySample, newestSample, resolveSample } from '../prompts/sample.js';
import SeenTab from '../prompts/SeenTab.jsx';
import TextsTab from '../prompts/TextsTab.jsx';
import '../prompts/prompts.css';

const TABS = [
  { id: 'profiles', label: 'Профили' },
  { id: 'rules', label: 'Правила' },
  { id: 'texts', label: 'Тексты' },
  { id: 'preview', label: 'Предпросмотр' },
  { id: 'seen', label: 'Что увидела модель' },
  { id: 'pins', label: 'Пины' },
];
const EDITING = ['profiles', 'rules', 'texts'];

function initialTab() {
  const id = location.hash.replace(/^#prompts\/?/, '');
  return TABS.some((tab) => tab.id === id) ? id : 'profiles';
}

// The longest data-path inside the screen that the relay's param points into: where to put the focus after a refused save.
function fieldFor(param) {
  const wanted = normalizePath(param);
  let best = null;
  for (const element of document.querySelectorAll('[data-path]')) {
    const path = normalizePath(element.getAttribute('data-path'));
    if (errorHits(wanted, path) && (!best || path.length > best.path.length)) best = { element, path };
  }
  return best?.element || null;
}

export default function PromptsView({ apiKey, onUnauthorized, unsaved }) {
  const toast = useToast();
  const [server, setServer] = useState(null);
  const [base, setBase] = useState(null);
  const [draft, setDraft] = useState(null);
  const [loadError, setLoadError] = useState('');
  const [saving, setSaving] = useState(false);
  const [failure, setFailure] = useState(null);
  // What the relay said about the document at the last save, for a relay that does not list warnings with the document itself
  const [savedWarnings, setSavedWarnings] = useState([]);
  const [tab, setTab] = useState(initialTab);
  const [selected, setSelected] = useState(null);
  const [expand, setExpand] = useState(null);
  const [sampleState, setSampleState] = useState(emptySample);
  const [recent, setRecent] = useState([]);
  const [recentError, setRecentError] = useState('');
  const [recentLoading, setRecentLoading] = useState(false);
  const draftRef = useRef(draft);
  const baseRef = useRef(base);
  draftRef.current = draft;
  baseRef.current = base;

  const adopt = useCallback((next) => {
    setBase({ document: next.document, rev: next.rev });
    setDraft(next.document);
    setFailure(null);
    setSavedWarnings([]);
  }, []);

  const refresh = useCallback(async ({ force = false } = {}) => {
    try {
      const next = await loadPrompts(apiKey);
      setServer(next);
      setLoadError('');
      const current = baseRef.current;
      if (!current || force) adopt(next);
      else if (next.rev !== current.rev && changedPaths(current.document, draftRef.current).length === 0) adopt(next);
    } catch (error) {
      if (error.status === 401) onUnauthorized();
      else setLoadError(error.message);
    }
  }, [apiKey, adopt, onUnauthorized]);
  usePolling(refresh, 15000, [apiKey]);

  const loadSamples = useCallback(async () => {
    setRecentLoading(true);
    try {
      setRecent(await loadRecent(apiKey, tab === 'seen' ? 50 : 10));
      setRecentError('');
    } catch (error) {
      if (error.status === 401) onUnauthorized();
      else setRecentError(error.message);
    } finally {
      setRecentLoading(false);
    }
  }, [apiKey, tab, onUnauthorized]);
  usePolling(loadSamples, tab === 'seen' ? 8000 : 60000, [apiKey, tab === 'seen']);

  const dirtyPaths = useMemo(() => (base ? changedPaths(base.document, draft) : []), [base, draft]);
  const dirty = dirtyPaths.length;
  useEffect(() => {
    if (!unsaved) return undefined;
    unsaved.current = dirty;
    return () => { unsaved.current = 0; };
  }, [dirty, unsaved]);

  useEffect(() => { history.replaceState(null, '', `#prompts/${tab}`); }, [tab]);

  // A recent request with a kept body is the sample until the user picks another.
  useEffect(() => {
    if (sampleState.mode !== 'recent' || sampleState.requestId) return;
    const first = newestSample(recent);
    if (first) setSampleState((state) => ({ ...state, requestId: first.request_id }));
  }, [recent, sampleState.mode, sampleState.requestId]);

  const recentKey = recent.map((item) => `${item.request_id}:${item.has_request}`).join('|');
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const resolved = useMemo(() => resolveSample(sampleState, recent), [sampleState, recentKey]);

  // A refused save is pointed out once: the next edit takes the mark away (a conflict stays until it is resolved)
  const setDoc = useCallback((update) => {
    setDraft((current) => update(current ?? blankDocument()));
    setFailure((current) => (current && current.status !== 409 ? null : current));
  }, []);
  const conflict = failure?.status === 409 || (!!server && !!base && server.rev !== base.rev && dirty > 0);

  async function save() {
    const submitted = draft;
    setSaving(true);
    setFailure(null);
    try {
      const result = await savePrompts(apiKey, submitted, base.rev);
      setBase({ document: submitted, rev: result.rev });
      setSavedWarnings(result.warnings);
      toast({ tone: 'success', title: 'Промпты сохранены', text: `Ревизия ${result.rev}. Новые диалоги получат её сразу, начатые остаются на своей` });
      const fresh = await loadPrompts(apiKey);
      setServer(fresh);
      if (fresh.rev === result.rev) {
        setBase({ document: fresh.document, rev: fresh.rev });
        // A document the relay stored as sent leaves the draft as it is, so open rules and the focus stay where they were
        setDraft((current) => (current === submitted && !equalJson(submitted, fresh.document) ? fresh.document : current));
      }
    } catch (error) {
      if (error.status === 401) { onUnauthorized(); return; }
      const reason = failureOf(error);
      setFailure(reason);
      if (reason.status === 409) refresh();
      if (reason.param) {
        const place = locate(reason.param);
        if (place.tab) setTab(place.tab);
        if (place.profile) setSelected(place.profile);
        if (place.rule !== undefined && place.rule !== null) setExpand(place.rule);
      }
    } finally {
      setSaving(false);
    }
  }

  // After a refused save the screen shows the field the relay names and puts the focus on it.
  useEffect(() => {
    if (!failure?.param) return undefined;
    const timer = setTimeout(() => {
      const field = fieldFor(failure.param);
      const target = field?.matches('input, textarea, select, button') ? field : field?.querySelector('input, textarea, select, button');
      target?.scrollIntoView({ block: 'center' });
      target?.focus({ preventScroll: true });
    }, 80);
    return () => clearTimeout(timer);
  }, [failure]);

  const reload = () => {
    if (dirty && !window.confirm('Загрузить документ заново? Несохранённые правки будут потеряны')) return;
    refresh({ force: true });
  };

  const addRuleFor = (profile) => {
    const rule = newRule(profile);
    setDoc((current) => ({ ...current, rules: insertAt(rulesOf(current), rulesOf(current).length, rule) }));
    setExpand(rulesOf(draft).length);
    setTab('rules');
  };

  const onKeyTab = (event, index) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? TABS.length - 1
      : (index + (event.key === 'ArrowRight' ? 1 : -1) + TABS.length) % TABS.length;
    setTab(TABS[next].id);
    document.getElementById(`prompts-tab-${TABS[next].id}`)?.focus();
  };

  const builtin = server?.builtin;
  // The relay lists its warnings with the document in force; the answer of the last save stands in when it does not
  const docWarnings = server?.hasWarnings ? server.warnings : savedWarnings;
  const profileNames = [...(builtin?.profiles?.length ? builtin.profiles : ['passthrough']), ...Object.keys(profilesOf(draft))];
  const counts = { profiles: Object.keys(profilesOf(draft)).length, rules: rulesOf(draft).length, texts: Object.keys(draft?.texts || {}).length, seen: recent.length };
  const live = { sampleState, setSampleState, resolved, recent, detected: sampleState.mode === 'paste' && !sampleState.dialect ? resolved.sample?.dialect : '', refreshRecent: loadSamples };
  const legacyMode = !!server && !draft;

  return (
    <Page className="prompts">
      <header className="page-head">
        <div>
          <h1>Промпты</h1>
          <p className="page-meta">Что видит модель: профили, правила, тексты relay, предпросмотр и закрепления</p>
        </div>
        <div className="head-actions">
          {server && (server.rev === null ? <Tag tone="warn" title="Файла промптов нет: действует прежний путь">legacy</Tag> : <Tag tone="accent">{`ревизия ${server.rev}`}</Tag>)}
          <Button icon={RefreshCw} onClick={reload}>Перезагрузить</Button>
        </div>
      </header>

      {loadError && <div className="banner tone-neg" role="alert">Не удалось загрузить промпты: {loadError}</div>}
      {!server && !loadError && <div className="panel empty" role="status">Загружаем промпты</div>}
      {server?.errors.length > 0 && (
        <Notice tone="neg" icon={TriangleAlert} role="alert" className="p-file-errors">
          <strong>Файл {server.path || 'prompts.json'} на диске не принят</strong>
          {server.rev === null ? '. Действует прежний режим' : `. Продолжает действовать ревизия ${server.rev}`}
          <ul>
            {server.errors.map((error, index) => <li key={index}>{error.message}{error.param && <> <code>{error.param}</code></>}</li>)}
          </ul>
        </Notice>
      )}
      {conflict && (
        <Notice tone="warn" icon={TriangleAlert} role="alert" className="p-conflict">
          <strong>Документ изменился, перезагрузите.</strong>{' '}
          Пока вы правили, документ промптов сохранили заново{server?.rev !== null && server?.rev !== undefined ? ` (сейчас ревизия ${server.rev})` : ''}.
          Ваши правки не сохранены, чтобы не затереть чужие. Загрузите актуальный документ и перенесите правки; черновик можно скопировать.
          <span className="head-actions">
            <Button icon={RefreshCw} onClick={() => refresh({ force: true })}>Загрузить актуальный документ</Button>
            <Button icon={Copy} onClick={() => navigator.clipboard?.writeText(JSON.stringify(draft, null, 2)).catch(() => {})}>Скопировать мой черновик</Button>
          </span>
        </Notice>
      )}
      {failure && failure.status !== 409 && (
        <Notice tone="neg" icon={TriangleAlert} role="alert" className="p-save-error">
          <strong>Не сохранено.</strong> {failureLead(failure)}: {failure.message}
          {failure.param && <> <code>{failure.param}</code></>}
        </Notice>
      )}
      {docWarnings.length > 0 && (
        <Notice tone="warn" icon={TriangleAlert} className="p-doc-warnings">
          <strong>Предупреждения relay о документе</strong>
          <ul>{docWarnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>
        </Notice>
      )}

      {server && (
        <>
          <div className="p-tabs-row">
            <div className="tabs p-tabs" role="tablist" aria-label="Разделы промптов">
              {TABS.map((item, index) => (
                <button key={item.id} id={`prompts-tab-${item.id}`} type="button" role="tab" aria-selected={tab === item.id} aria-controls={`prompts-panel-${item.id}`}
                  tabIndex={tab === item.id ? 0 : -1} onClick={() => setTab(item.id)} onKeyDown={(event) => onKeyTab(event, index)}>
                  {item.label}{counts[item.id] > 0 && <span className="chip-count num">{fmtInt(counts[item.id])}</span>}
                </button>
              ))}
            </div>
          </div>
          <ErrorContext.Provider value={failure?.param ? failure : null}>
            {TABS.map((item) => (
              <div key={item.id} role="tabpanel" id={`prompts-panel-${item.id}`} aria-labelledby={`prompts-tab-${item.id}`} hidden={tab !== item.id}>
                {tab === item.id && (
                  EDITING.includes(item.id) && legacyMode ? (
                    <LegacyStart legacy={server.legacy} builtin={builtin} path={server.path} invalid={server.errors.length > 0} onCreate={(name, starter) => {
                      setDraft(addProfile(blankDocument(), name, starterProfile(starter)));
                      setSelected(name);
                      setTab('profiles');
                    }} />
                  ) : item.id === 'profiles' ? (
                    <ProfilesTab apiKey={apiKey} doc={draft} setDoc={setDoc} builtin={builtin} selected={selected} select={setSelected}
                      onAddRule={addRuleFor} live={live} onUnauthorized={onUnauthorized} />
                  ) : item.id === 'rules' ? (
                    <RulesTab doc={draft} setDoc={setDoc} builtin={builtin} profileNames={profileNames} expandIndex={expand} onExpanded={() => setExpand(null)} />
                  ) : item.id === 'texts' ? (
                    <TextsTab doc={draft} setDoc={setDoc} builtin={builtin} />
                  ) : item.id === 'preview' ? (
                    <PreviewTab apiKey={apiKey} doc={draft} dirty={dirty > 0} {...live} onUnauthorized={onUnauthorized} />
                  ) : item.id === 'seen' ? (
                    <SeenTab items={recent} error={recentError} loading={recentLoading} refresh={loadSamples}
                      onPreview={(entry) => { setSampleState((state) => ({ ...state, mode: 'recent', requestId: entry.request_id })); setTab('preview'); }} />
                  ) : (
                    <PinsTab apiKey={apiKey} rev={server.rev} onUnauthorized={onUnauthorized} />
                  )
                )}
              </div>
            ))}
          </ErrorContext.Provider>
        </>
      )}

      <AnimatePresence>
        {dirty > 0 && (
          <motion.div className="savebar" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 8 }} transition={{ duration: 0.2, ease }}>
            <span><b className="num">{dirty}</b> несохранённых: <span className="savebar-keys" title={dirtyPaths.map(pathLabel).join(', ')}>{dirtyPaths.map(pathLabel).join(', ')}</span></span>
            <Button icon={RotateCcw} onClick={() => { setDraft(base.document); setFailure(null); }}>Отменить</Button>
            <Button icon={Save} tone="primary" onClick={save} disabled={saving}>{saving ? 'Сохранение' : 'Сохранить'}</Button>
          </motion.div>
        )}
      </AnimatePresence>
    </Page>
  );
}
