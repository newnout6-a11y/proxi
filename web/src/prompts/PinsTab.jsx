import { useEffect, useRef, useState } from 'react';
import { PinOff, RefreshCw, TriangleAlert } from 'lucide-react';
import { Button, Empty, Tag, useToast } from '../ui.jsx';
import { usePolling } from '../useJournal.js';
import { fmtAgo, fmtInt, plural } from '../format.js';
import { changePins, failureOf, loadPins } from './api.js';
import { Notice } from './controls.jsx';
import { useFocusLater } from './hooks.js';

/** Conversations held on the revision they started with, until they are unpinned or everything is applied at once. */
export default function PinsTab({ apiKey, rev, onUnauthorized }) {
  const toast = useToast();
  const [pins, setPins] = useState(null);
  const [error, setError] = useState('');
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const heading = useRef(null);
  const root = useRef(null);
  const focusLater = useFocusLater();

  const load = async () => {
    try {
      setPins(await loadPins(apiKey));
      setError('');
    } catch (failure) {
      if (failure.status === 401) onUnauthorized();
      else setError(failure.message);
    }
  };
  usePolling(load, 10000, [apiKey]);
  useEffect(() => { if (confirming) heading.current?.focus(); }, [confirming]);

  async function run(action, conversation) {
    setBusy(true);
    // The row and the panel that were pressed go away with the answer: the focus waits on the button that never does
    root.current?.querySelector('[data-act="refresh"]')?.focus();
    try {
      const result = await changePins(apiKey, action, conversation);
      const count = Number.isFinite(result?.unpinned) ? result.unpinned : 0;
      toast(action === 'apply_all'
        ? { tone: 'success', title: 'Ревизия применена ко всем', text: `Снято закреплений: ${fmtInt(count)}. Диалоги получат текущую ревизию на следующем ходу` }
        : { tone: 'success', title: 'Диалог откреплён', text: 'Со следующего хода он получит текущую ревизию' });
      setConfirming(false);
      await load();
    } catch (failure) {
      if (failure.status === 401) onUnauthorized();
      else toast({ tone: 'error', title: 'Не удалось изменить закрепления', text: failureOf(failure).message });
    } finally {
      setBusy(false);
    }
  }

  const items = pins || [];
  // A conversation is pinned once for each purpose it has, and an unpin or an apply takes all of them: it is the conversations that are counted
  const conversations = new Set(items.map((pin) => pin.conversation)).size;
  const now = Date.now() / 1000;
  return (
    <section className="panel p-pins" aria-label="Закреплённые диалоги" ref={root}>
      <div className="panel-head">
        <div>
          <h2>Закреплённые диалоги</h2>
          <p className="panel-meta">Диалог остаётся на ревизии, с которой начался: смена head посреди диалога сбрасывает кэш. Закрепления сохраняются рядом с состоянием диалогов и переживают перезапуск, если сохранение состояния включено; иначе они живут в памяти relay</p>
        </div>
        <div className="head-actions">
          <Button icon={RefreshCw} data-act="refresh" onClick={load}>Обновить</Button>
          <Button tone="primary" data-act="apply-all" disabled={!items.length || busy || confirming} onClick={() => setConfirming(true)}>Применить ко всем</Button>
        </div>
      </div>
      <p className="p-hint">
        Прежний путь без профилей (роль и режим её применения в «Настройках») меняется на лету и сразу для всех его диалогов: закрепления его
        не удерживают, кэш промпта активных диалогов будет записан заново
      </p>
      {error && <p className="banner tone-neg" role="alert">Не удалось загрузить: {error}</p>}
      {confirming && (
        <Notice tone="warn" icon={TriangleAlert} role="group" className="p-confirm">
          <h3 tabIndex={-1} ref={heading}>Применить текущую ревизию ко всем диалогам?</h3>
          <p>
            Закрепления будут сняты у {fmtInt(conversations)} {plural(conversations, ['диалога', 'диалогов', 'диалогов'])}, и на следующем ходу каждый получит ревизию
            {rev === null ? '' : ` ${rev}`}. Это не бесплатно: смена head, истории или инструментов посреди диалога сбрасывает кэш промпта и подписи thinking,
            поэтому следующий ход каждого активного диалога заплатит за полную запись кэша. На большом диалоге это порядка 10–44 кредитов
          </p>
          <div className="head-actions">
            <Button tone="primary" disabled={busy} onClick={() => run('apply_all')}>{busy ? 'Применяем' : 'Да, применить ко всем'}</Button>
            <Button disabled={busy} onClick={() => { setConfirming(false); focusLater(() => root.current?.querySelector('[data-act="apply-all"]')); }}>Отмена</Button>
          </div>
        </Notice>
      )}
      {pins && !items.length && !error && (
        <Empty title="Закреплённых диалогов нет" text="Диалог закрепляется за ревизией, когда relay принимает его запрос с conversation key. Новые диалоги получают текущую ревизию" />
      )}
      {items.length > 0 && (
        <ul className="pin-list">
          <li className="pin-cols" aria-hidden="true"><span>диалог</span><span>назначение</span><span>профиль</span><span>ревизия</span><span>возраст</span><span /></li>
          {items.map((pin) => {
            // an unpin names the conversation, not one of its purposes: the relay takes every pin the conversation has
            const sameConversation = items.filter((other) => other.conversation === pin.conversation).length;
            return (
              <li className="pin-row" key={`${pin.conversation}-${pin.purpose}`}>
                <span className="mono pin-conversation" title="Тег диалога: не ключ">{pin.conversation || '—'}</span>
                <span className="mono">{pin.purpose || '—'}</span>
                <span className="mono">{pin.profile ?? 'legacy'}</span>
                <span>
                  <Tag tone={rev !== null && pin.rev === rev ? 'pos' : 'warn'} title={rev !== null && pin.rev === rev ? 'Совпадает с текущей ревизией' : 'Отличается от текущей ревизии: диалог не видит новых правок'}>
                    {pin.rev === 0 ? 'без файла' : String(pin.rev ?? '—')}
                  </Tag>
                </span>
                <span className="mono muted">{pin.age_s === null ? '—' : fmtAgo(now - pin.age_s, now)}</span>
                <Button icon={PinOff} disabled={busy} aria-label={`Открепить диалог ${pin.conversation}`}
                  title={sameConversation > 1 ? `Снимет все закрепления этого диалога (${sameConversation}), не только это назначение` : undefined}
                  onClick={() => run('unpin', pin.conversation)}>Открепить</Button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
