import { useMemo, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Boxes, Brain, Search, Wrench } from 'lucide-react';
import { api } from '../api.js';
import { usePolling } from '../useJournal.js';
import { Page, Reveal, spring } from '../motion.jsx';
import { Badge, Empty } from '../ui.jsx';
import { fmtTokens } from '../format.js';

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

  const items = useMemo(() => (data?.data || []).filter((model) => (
    (!onlyRoutable || model.routable)
    && (!query || `${model.id} ${model.label} ${model.provider}`.toLowerCase().includes(query.toLowerCase()))
  )), [data, query, onlyRoutable]);

  return (
    <Page className="models">
      <Reveal as="header" className="page-head">
        <div>
          <p className="eyebrow">Каталог · {data?.data?.length || 0} моделей</p>
          <h1>Модели</h1>
        </div>
        <div className="head-actions">
          <div className="search compact">
            <Search size={15} aria-hidden="true" />
            <input aria-label="Поиск модели" placeholder="Поиск модели" value={query} onChange={(e) => setQuery(e.target.value)} />
          </div>
          <label className="check"><input type="checkbox" checked={onlyRoutable} onChange={(e) => setOnlyRoutable(e.target.checked)} /><span>Только с маршрутом</span></label>
        </div>
      </Reveal>
      {error && <div className="banner tone-danger">{error}</div>}
      <motion.div className="model-grid" layout>
        <AnimatePresence>
          {items.map((model, index) => (
            <motion.article key={model.id} layout className={`model-card ${model.id === data?.default_model ? 'is-default' : ''}`}
              initial={{ opacity: 0, y: 20, scale: 0.96 }} animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, scale: 0.9 }} transition={{ ...spring, delay: Math.min(index, 20) * 0.02 }}
              whileHover={{ y: -5 }}>
              <div className="model-top">
                <span className={`provider p-${model.provider}`}>{model.provider}</span>
                {model.id === data?.default_model && <Badge tone="accent">по умолчанию</Badge>}
              </div>
              <h3>{model.label}</h3>
              <code className="muted">{model.id}</code>
              <dl className="model-stats">
                <div><dt>контекст</dt><dd className="num">{fmtTokens(model.context)}</dd></div>
                <div><dt>вывод</dt><dd className="num">{fmtTokens(model.max_output)}</dd></div>
              </dl>
              <div className="model-tags">
                {model.reasoning && <Badge tone="info"><Brain size={12} /> {(model.reasoning_effort_levels || []).join(' · ') || 'reasoning'}</Badge>}
                {model.tools && <Badge><Wrench size={12} /> tools</Badge>}
                <Badge tone={model.routable ? 'success' : 'neutral'}>{model.routable ? 'маршрут есть' : 'нет ресурса'}</Badge>
                {model.metadata_status && model.metadata_status !== 'verified' && <Badge tone="warn">{model.metadata_status}</Badge>}
              </div>
            </motion.article>
          ))}
        </AnimatePresence>
      </motion.div>
      {data && !items.length && <Empty icon={Boxes} title="Модели не найдены" />}
    </Page>
  );
}
