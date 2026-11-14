import { useMemo, useState } from 'react';
import { TriangleAlert } from 'lucide-react';
import { BASELINE_DOCUMENT, forceProfile, previewRequest } from './api.js';
import { Disclosure, Notice } from './controls.jsx';
import { diffLines } from './diff.js';
import DiffView from './DiffView.jsx';
import HeadView from './HeadView.jsx';
import { usePreview } from './hooks.js';
import { PreviewFacts, PreviewLists } from './PreviewResult.jsx';
import SampleInput from './SampleInput.jsx';

/**
 * What the profile under edit does to a sample request, judged by the draft and refreshed as it is typed.
 * The profile is applied to the sample whatever the rules say; the same request through the built-in passthrough
 * profile is the baseline, so the difference between them is what the profile cut and what it added.
 */
export default function LivePreview({ apiKey, doc, profile, sampleState, setSampleState, resolved, recent, detected, refreshRecent, onUnauthorized }) {
  const sample = resolved.sample;
  const draftBody = useMemo(() => (sample ? previewRequest(sample, forceProfile(doc, profile)) : null), [sample, doc, profile]);
  const baseBody = useMemo(() => (sample ? previewRequest(sample, BASELINE_DOCUMENT) : null), [sample]);
  const live = usePreview(apiKey, draftBody, { onUnauthorized });
  const base = usePreview(apiKey, baseBody, { delay: 0, onUnauthorized });
  const [view, setView] = useState('cut');
  // The sample form folds away once a sample is chosen, and opens again while there is none, until the user decides
  const [folded, setFolded] = useState(null);
  const rows = useMemo(() => (live.data && base.data ? diffLines(base.data.head, live.data.head) : []), [live.data, base.data]);
  const data = live.data;

  return (
    <section className="panel p-live" aria-label="Что получит модель">
      <div className="panel-head">
        <h2>Что получит модель</h2>
        {live.loading && <span className="muted small" role="status">обновляем</span>}
      </div>
      <Disclosure title="Образец запроса" meta={sample ? sample.label : 'не выбран'} open={folded === null ? !sample || sampleState.mode === 'paste' : !folded} onOpenChange={(open) => setFolded(!open)}>
        <SampleInput value={sampleState} onChange={setSampleState} recent={recent} error={resolved.error} detected={detected} onRefresh={refreshRecent} legendHidden />
      </Disclosure>
      {!sample && !resolved.error && (
        <p className="p-hint">Выберите образец: профиль применится к нему, и здесь появится то, что он вырезал и добавил. Живой предпросмотр работает по черновику, до сохранения</p>
      )}
      {live.error && (
        <Notice tone="neg" icon={TriangleAlert} role="alert">
          {live.error.status === 400 ? 'Черновик не принят relay' : 'Предпросмотр не получен'}: {live.error.message}{live.error.param ? ` (${live.error.param})` : ''}
        </Notice>
      )}
      {data && (
        <div className="p-live-body" aria-busy={live.loading}>
          <PreviewFacts data={data} forced />
          <div className="segmented" role="group" aria-label="Что показать">
            <button type="button" aria-pressed={view === 'cut'} onClick={() => setView('cut')}>Что вырезано</button>
            <button type="button" aria-pressed={view === 'head'} onClick={() => setView('head')}>Итоговый head</button>
          </div>
          {view === 'cut' ? (
            base.data ? (
              <DiffView rows={rows} label="Что вырезано и что добавлено" emptyText="Head пуст"
                unchangedText="Head совпадает с system клиента: профиль ничего не вырезал и не добавил" />
            ) : (
              <p className="muted small p-empty-line">{base.error ? `Не удалось получить исходный head: ${base.error.message}` : 'Считаем исходный head'}</p>
            )
          ) : (
            <HeadView head={data.head} parts={data.parts} label="Итоговый head профиля" />
          )}
          <PreviewLists data={data} />
        </div>
      )}
    </section>
  );
}
