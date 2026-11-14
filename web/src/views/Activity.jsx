import { useEffect, useState } from 'react';
import { Page } from '../motion.jsx';
import { buildTraces } from '../turns.js';
import { fmtInt } from '../format.js';
import Journal from './Journal.jsx';
import Turns from './Turns.jsx';

const TABS = [
  { id: 'turns', label: 'Ходы моделей' },
  { id: 'events', label: 'Все события' },
];

function initialTab() {
  return location.hash === '#activity/events' ? 'events' : 'turns';
}

/** Turns and the raw journal are one stream of activity: a turn is a group of journal events. */
export default function Activity(props) {
  const [tab, setTab] = useState(initialTab);
  useEffect(() => { history.replaceState(null, '', `#activity/${tab}`); }, [tab]);
  const turns = buildTraces(props.journal.events).length;
  const counts = { turns, events: props.journal.events.length };
  return (
    <Page className="activity">
      <header className="page-head">
        <div>
          <h1>Активность</h1>
          <p className="page-meta">ход — это группа событий журнала от запроса до ответа</p>
        </div>
        <div className="tabs" role="tablist" aria-label="Вид активности">
          {TABS.map((item) => (
            <button key={item.id} id={`activity-tab-${item.id}`} type="button" role="tab" aria-selected={tab === item.id}
              aria-controls={`activity-panel-${item.id}`} tabIndex={tab === item.id ? 0 : -1} onClick={() => setTab(item.id)}
              onKeyDown={(event) => {
                if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
                event.preventDefault();
                const next = event.key === 'Home' ? 'turns' : event.key === 'End' ? 'events' : tab === 'turns' ? 'events' : 'turns';
                setTab(next);
                document.getElementById(`activity-tab-${next}`)?.focus();
              }}>
              {item.label}<span className="chip-count num">{fmtInt(counts[item.id])}</span>
            </button>
          ))}
        </div>
      </header>
      <div role="tabpanel" id={`activity-panel-${tab}`} aria-labelledby={`activity-tab-${tab}`} tabIndex={0}>
        {tab === 'turns' ? <Turns {...props} embedded /> : <Journal {...props} embedded />}
      </div>
    </Page>
  );
}
