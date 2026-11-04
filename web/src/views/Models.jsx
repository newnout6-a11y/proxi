import { useMemo, useState } from 'react';
import { Search } from 'lucide-react';
import { api } from '../api.js';
import { usePolling } from '../useJournal.js';
import { Dot, Page } from '../motion.jsx';
import { Empty, Tag } from '../ui.jsx';
import { fmtInt, fmtTokens } from '../format.js';

export default function Models({ apiKey, onUnauthorized }) {
  const [data, setData] = useState(null);
  const [query, setQuery] = useState('');
  const [onlyRoutable, setOnlyRoutable] = useState(false);
  const [error, setError] = useState('');

  usePolling(async () => {
    try {
      setData(await api(apiKey, 'models'));
      setError('');
    } catch (failure) {
      if (failure.status === 401) onUnauthorized(); else setError(failure.message);
    }
  }, 60000, [apiKey]);

  const all = data?.data || [];
  const items = useMemo(() => all.filter((model) => (
    (!onlyRoutable || model.routable)
    && (!query || `${model.id} ${model.label} ${model.provider}`.toLowerCase().includes(query.toLowerCase()))
  )), [all, query, onlyRoutable]);
  const routable = all.filter((model) => model.routable).length;

  return (
    <Page className="models">
      <header className="page-head">
        <div>
          <h1>Модели</h1>
          <p className="page-meta">в каталоге {fmtInt(all.length)} · с маршрутом {fmtInt(routable)}</p>
        </div>
        <div className="head-actions">
          <div className="search compact">
            <Search size={16} strokeWidth={1.5} aria-hidden="true" />
            <input aria-label="Поиск модели" placeholder="Поиск" value={query} onChange={(e) => setQuery(e.target.value)} />
          </div>
          <label className="check">
            <input type="checkbox" checked={onlyRoutable} onChange={(e) => setOnlyRoutable(e.target.checked)} />
            <span>Только с маршрутом</span>
          </label>
        </div>
      </header>
      {error && <div className="banner tone-neg" role="alert">{error}</div>}
      <section className="panel model-table">
        {items.length > 0 && (
          <div className="model-cols" aria-hidden="true">
            <span>модель</span><span>провайдер</span><span>контекст</span><span>вывод</span><span>рассуждения</span><span>маршрут</span>
          </div>
        )}
        {items.map((model) => (
          <article key={model.id} className={`model-card ${model.id === data?.default_model ? 'is-default' : ''}`}>
            <span className="model-name">
              <strong>{model.label}</strong>
              <code>{model.id}</code>
            </span>
            <span className="model-provider">{model.provider}</span>
            <span className="num">{fmtTokens(model.context)}</span>
            <span className="num">{fmtTokens(model.max_output)}</span>
            <span className="model-effort">
              {model.reasoning ? (model.reasoning_effort_levels || []).join(' · ') || 'есть' : 'нет'}
              {model.tools && <Tag>инструменты</Tag>}
            </span>
            <span className="model-route">
              <Dot tone={model.routable ? 'pos' : 'muted'} />
              {model.routable ? 'есть' : 'нет ресурса'}
              {model.id === data?.default_model && <Tag tone="accent">по умолчанию</Tag>}
              {model.metadata_status && model.metadata_status !== 'verified' && <Tag tone="warn">{model.metadata_status}</Tag>}
            </span>
          </article>
        ))}
        {data && !items.length && <Empty title="Модели не найдены" text="Измените запрос или снимите фильтр" />}
      </section>
    </Page>
  );
}
