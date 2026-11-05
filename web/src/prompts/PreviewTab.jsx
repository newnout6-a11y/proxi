import { useMemo, useState } from 'react';
import { previewRequest } from './api.js';
import { usePreview } from './hooks.js';
import HeadView from './HeadView.jsx';
import { PreviewFacts, PreviewLists } from './PreviewResult.jsx';
import SampleInput from './SampleInput.jsx';
import { Notice } from './controls.jsx';
import { TriangleAlert } from 'lucide-react';

/** One request through the whole document: which rule and profile took it, the head the model gets and what was changed. */
export default function PreviewTab({ apiKey, doc, dirty, sampleState, setSampleState, resolved, recent, detected, refreshRecent, onUnauthorized }) {
  const [source, setSource] = useState('draft');
  const judged = dirty && source === 'draft';
  const judgedDoc = judged ? doc : undefined;
  const body = useMemo(() => (resolved.sample ? previewRequest(resolved.sample, judgedDoc) : null), [resolved.sample, judgedDoc]);
  const { data, error, loading } = usePreview(apiKey, body, { onUnauthorized });

  return (
    <div className="p-preview">
      <section className="panel" aria-label="Образец">
        <SampleInput value={sampleState} onChange={setSampleState} recent={recent} error={resolved.error} detected={detected} onRefresh={refreshRecent} />
        {dirty && (
          <div className="p-source">
            <span className="p-label" id="preview-source">Документ</span>
            <div className="segmented" role="group" aria-labelledby="preview-source">
              <button type="button" aria-pressed={source === 'draft'} onClick={() => setSource('draft')}>Черновик</button>
              <button type="button" aria-pressed={source === 'saved'} onClick={() => setSource('saved')}>Сохранённая ревизия</button>
            </div>
            <p className="p-hint">{judged ? 'Предпросмотр учитывает несохранённые правки' : 'Предпросмотр по тому, что действует сейчас, без несохранённых правок'}</p>
          </div>
        )}
      </section>
      <section className="panel p-result" aria-label="Результат предпросмотра" aria-busy={loading}>
        <div className="panel-head">
          <h2>Что получит модель</h2>
          {loading && <span className="muted small" role="status">обновляем</span>}
        </div>
        {!resolved.sample && <p className="p-hint">Выберите последний запрос или вставьте тело: здесь появится итоговый head и то, что с ним сделали правила и профиль</p>}
        {error && (
          <Notice tone="neg" icon={TriangleAlert} role="alert">
            {error.status === 400 ? 'Документ не принят relay' : 'Предпросмотр не получен'}: {error.message}{error.param ? ` (${error.param})` : ''}
          </Notice>
        )}
        {data && (
          <>
            <PreviewFacts data={data} />
            <HeadView head={data.head} parts={data.parts} tall />
            <PreviewLists data={data} />
          </>
        )}
      </section>
    </div>
  );
}
