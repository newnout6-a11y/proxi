import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, ChevronRight, GripVertical, Lock, Plus, ShieldAlert, Trash, TriangleAlert } from 'lucide-react';
import { Button, Tag, Toggle } from '../ui.jsx';
import Conditions from './Conditions.jsx';
import { Check, IconBtn, Line, Notice, Select } from './controls.jsx';
import { summarizeConditions } from './conditions.js';
import { catchAllIndex, idOf, insertAt, moveItem, newRule, protectedEnabled, removeAt, rulesOf, setProtected, setRuleField, updateAt } from './draft.js';
import { useAnnouncer } from './hooks.js';

const HELP_ID = 'rules-move-help';

const targetLabel = (use) => use || 'профиль не выбран';

function RuleEditor({ rule, path, specs, profileNames, specialUses, onChange }) {
  const options = [
    ...(rule.use ? [] : [{ value: '', label: 'Выберите профиль' }]),
    ...profileNames.map((name) => ({ value: name, label: name })),
    ...specialUses.map((item) => ({ value: item.id, label: item.label && item.label !== item.id ? `${item.id} — ${item.label}` : item.id })),
  ];
  if (rule.use && !options.some((option) => option.value === rule.use)) options.push({ value: rule.use, label: `${rule.use} — такого профиля нет` });
  const special = specialUses.find((item) => item.id === rule.use);
  return (
    <div className="rule-editor">
      <Line label="Название" value={rule.name || ''} mono={false} path={`${path}.name`} hint="Показывается в предпросмотре и журнале"
        onChange={(name) => onChange((current) => setRuleField(current, 'name', name))} />
      <Select label="Применить профиль" value={rule.use || ''} options={options} path={`${path}.use`} hint={special?.hint}
        onChange={(use) => onChange((current) => setRuleField(current, 'use', use))} />
      <h4 className="p-subhead">Условия</h4>
      <Conditions value={rule.if} specs={specs} path={`${path}.if`} onChange={(next) => onChange((current) => ({ ...current, if: next }))} />
      <Check label="Применять и к защищённым запросам (override_protected)" checked={rule.override_protected === true} path={`${path}.override_protected`}
        onChange={(on) => onChange((current) => setRuleField(current, 'override_protected', on))} />
      {rule.override_protected === true && (
        <Notice tone="warn" icon={ShieldAlert} role="alert">
          Правило сработает и на служебных запросах: заголовках диалогов, сжатии контекста, проверках соединения. Они получат ваш профиль вместо
          system клиента, и у IDE могут сломаться заголовки чатов и сжатие
        </Notice>
      )}
    </div>
  );
}

function Protection({ doc, setDoc, builtin }) {
  const enabled = protectedEnabled(doc);
  return (
    <section className="panel p-protect" aria-label="Защитные правила">
      <div className="panel-head">
        <h2><Lock size={16} strokeWidth={1.5} aria-hidden="true" />Защитные правила</h2>
        <div className="p-switch-line">
          <span>{enabled ? 'Защита включена' : 'Защита выключена'}</span>
          <Toggle id="protect-toggle" checked={enabled} label="Защита служебных запросов" onChange={(on) => setDoc((current) => setProtected(current, on))} />
        </div>
      </div>
      <p className="p-hint">
        Служебные запросы IDE идут с system клиента как есть: заголовки диалогов, сжатие, проверки соединения. Встроенные правила стоят выше ваших
        и не меняются
      </p>
      {!enabled && (
        <Notice tone="warn" icon={TriangleAlert} role="alert">
          Защита выключена. Заголовки диалогов, сжатие контекста и проверки соединения получат ваш профиль, как обычные запросы.
          У OpenCode и Claude Code это может сломать названия чатов и сжатие. Верните защиту, если не уверены
        </Notice>
      )}
      <ul className={`rule-list locked${enabled ? '' : ' is-off'}`} aria-label="Встроенные защитные правила">
        {builtin.protectedRules.map((rule) => (
          <li key={rule.name} className="rule rule-locked">
            <div className="rule-head">
              <span className="rule-no" aria-hidden="true"><Lock size={14} strokeWidth={1.5} /></span>
              <span className="rule-title mono">{rule.name}</span>
              <span className="rule-sub">{summarizeConditions(rule.if, builtin.conditions)} → {rule.use}</span>
              <Tag>{enabled ? 'встроенное' : 'не действует'}</Tag>
            </div>
          </li>
        ))}
        {!builtin.protectedRules.length && <li className="p-hint">Relay не сообщил список защитных правил</li>}
      </ul>
    </section>
  );
}

/**
 * The user's rules in the order they are tried, the first enabled match wins. A rule is reordered by dragging its
 * grip or with its Up and Down buttons (Alt+arrows on the row work too); every move is announced.
 */
export default function RulesTab({ doc, setDoc, builtin, profileNames, expandIndex, onExpanded }) {
  const rules = rulesOf(doc);
  const [open, setOpen] = useState(() => new Set());
  const [drag, setDrag] = useState(null);
  const [status, announce] = useAnnouncer();
  const rows = useRef(new Map());
  const focus = useRef(null);
  const toggleOpen = (id) => setOpen((previous) => { const next = new Set(previous); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const unreachableFrom = catchAllIndex(rules);

  useEffect(() => {
    if (expandIndex === null || expandIndex === undefined || !rules[expandIndex]) return;
    const id = idOf(rules[expandIndex]);
    setOpen((previous) => new Set(previous).add(id));
    rows.current.get(id)?.scrollIntoView({ block: 'nearest' });
    onExpanded?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expandIndex]);

  useLayoutEffect(() => {
    const target = focus.current;
    if (!target) return;
    focus.current = null;
    const row = rows.current.get(target.id);
    const pick = (act) => row?.querySelector(`[data-act="${act}"]:not(:disabled)`);
    // A rule that reached the end can only go the other way: the focus follows it there instead of getting lost
    (pick(target.act) || pick({ up: 'down', down: 'up' }[target.act]) || row?.querySelector('[data-act="main"]'))?.focus();
  });

  useEffect(() => {
    if (!drag) return undefined;
    const onKey = (event) => { if (event.key === 'Escape') setDrag(null); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [drag !== null]); // eslint-disable-line react-hooks/exhaustive-deps

  const move = (from, to, act) => {
    if (to < 0 || to >= rules.length || from === to) return;
    const rule = rules[from];
    focus.current = { id: idOf(rule), act: act || (to < from ? 'up' : 'down') };
    setDoc((current) => ({ ...current, rules: moveItem(rulesOf(current), from, to) }));
    announce(`Правило «${rule.name || `номер ${from + 1}`}»: позиция ${to + 1} из ${rules.length}`);
  };
  const patchRule = (index) => (change) => setDoc((current) => ({ ...current, rules: updateAt(rulesOf(current), index, change) }));
  const remove = (index) => {
    const name = rules[index].name || `номер ${index + 1}`;
    setDoc((current) => ({ ...current, rules: removeAt(rulesOf(current), index) }));
    announce(`Правило «${name}» удалено из черновика`);
  };
  const add = () => {
    const rule = newRule(profileNames.find((name) => name !== 'passthrough') || '');
    setDoc((current) => ({ ...current, rules: insertAt(rulesOf(current), rulesOf(current).length, rule) }));
    setOpen((previous) => new Set(previous).add(idOf(rule)));
    announce('Добавлено новое правило в конец списка');
  };

  // Where a dragged rule would land: the number of rows whose middle is above the pointer.
  const slotAt = (y) => rules.reduce((slot, rule) => {
    const box = rows.current.get(idOf(rule))?.getBoundingClientRect();
    return box && y >= box.top + box.height / 2 ? slot + 1 : slot;
  }, 0);
  const landing = drag ? (drag.slot > drag.from ? drag.slot - 1 : drag.slot) : null;
  const endDrag = (commit) => {
    if (drag && commit && landing !== drag.from) move(drag.from, landing, 'main');
    setDrag(null);
  };

  return (
    <div className="p-rules">
      <Protection doc={doc} setDoc={setDoc} builtin={builtin} />
      <section className="panel p-rules-panel" aria-label="Ваши правила">
        <div className="panel-head">
          <div>
            <h2>Ваши правила</h2>
            <p className="panel-meta">Запрос проверяется сверху вниз: побеждает первое включённое правило, подошедшее по условиям</p>
          </div>
          <Button icon={Plus} onClick={add}>Добавить правило</Button>
        </div>
        <p className="p-hint">Порядок меняют перетаскиванием за ручку слева или кнопками «Выше» и «Ниже». С клавиатуры: Alt и стрелка вверх или вниз на названии правила</p>
        <span className="p-sr" id={HELP_ID}>Alt и стрелка вверх или вниз перемещают правило</span>
        <div className="p-sr" role="status" aria-live="polite">{status}</div>
        {!rules.length && <p className="p-empty-line muted">Правил пока нет: запросы получат профиль по умолчанию, а если его нет — прежний путь</p>}
        <ol className={`rule-list${drag ? ' is-dragging-list' : ''}`} aria-label="Правила в порядке применения">
          {rules.map((rule, index) => {
            const id = idOf(rule);
            const isOpen = open.has(id);
            const name = rule.name || `Правило ${index + 1}`;
            const enabled = rule.enabled !== false;
            const dropping = drag && landing !== drag.from ? (drag.slot === index ? 'before' : drag.slot === rules.length && index === rules.length - 1 ? 'after' : null) : null;
            return (
              <li key={id} ref={(element) => { if (element) rows.current.set(id, element); else rows.current.delete(id); }}
                className={`rule${enabled ? '' : ' is-off'}${drag?.from === index ? ' is-dragging' : ''}`} data-drop={dropping || undefined} data-rule={index}>
                <div className="rule-head"
                  onKeyDown={(event) => {
                    if (!event.altKey || !['ArrowUp', 'ArrowDown'].includes(event.key)) return;
                    event.preventDefault();
                    move(index, index + (event.key === 'ArrowUp' ? -1 : 1), 'main');
                  }}>
                  <span className="rule-no num" aria-hidden="true">{index + 1}</span>
                  <span className="rule-grip" aria-hidden="true" title="Перетащить"
                    onPointerDown={(event) => {
                      if (event.pointerType === 'mouse' && event.button !== 0) return;
                      event.currentTarget.setPointerCapture(event.pointerId);
                      setDrag({ from: index, slot: index });
                    }}
                    onPointerMove={(event) => { if (drag) { const slot = slotAt(event.clientY); if (slot !== drag.slot) setDrag({ ...drag, slot }); } }}
                    onPointerUp={() => endDrag(true)} onPointerCancel={() => endDrag(false)}>
                    <GripVertical size={16} strokeWidth={1.5} />
                  </span>
                  <Toggle id={`rule-on-${id}`} checked={enabled} label={`Правило ${name} включено`}
                    onChange={(on) => patchRule(index)((current) => setRuleField(current, 'enabled', on))} />
                  <button type="button" className="rule-main" data-act="main" aria-expanded={isOpen} aria-controls={`rule-body-${id}`}
                    aria-describedby={HELP_ID} aria-keyshortcuts="Alt+ArrowUp Alt+ArrowDown" onClick={() => toggleOpen(id)}>
                    <ChevronRight className="chev" size={16} strokeWidth={1.5} aria-hidden="true" />
                    <span className="rule-title">{name}</span>
                    <span className="rule-sub">{summarizeConditions(rule.if, builtin.conditions)} → {targetLabel(rule.use)}</span>
                  </button>
                  <span className="rule-tags">
                    {!enabled && <Tag>выключено</Tag>}
                    {rule.override_protected === true && <Tag tone="warn" title="Правило сработает и на защищённых запросах">обходит защиту</Tag>}
                    {unreachableFrom >= 0 && index > unreachableFrom && enabled && <Tag tone="warn" title={`Правило ${unreachableFrom + 1} подходит под любой запрос`}>недостижимо</Tag>}
                  </span>
                  <span className="rule-tools">
                    <IconBtn icon={ArrowUp} label={`Выше: правило ${name}`} disabled={index === 0} data-act="up" onClick={() => move(index, index - 1)} />
                    <IconBtn icon={ArrowDown} label={`Ниже: правило ${name}`} disabled={index === rules.length - 1} data-act="down" onClick={() => move(index, index + 1)} />
                    <IconBtn icon={Trash} tone="danger" label={`Удалить правило ${name}`} onClick={() => remove(index)} />
                  </span>
                </div>
                {isOpen && (
                  <div className="rule-body" id={`rule-body-${id}`}>
                    <RuleEditor rule={rule} path={`rules.${index}`} specs={builtin.conditions} profileNames={profileNames} specialUses={builtin.specialUses} onChange={patchRule(index)} />
                  </div>
                )}
              </li>
            );
          })}
        </ol>
        <div className="p-default">
          <Select label="Если ни одно правило не подошло" value={doc.default_profile || ''} path="default_profile"
            onChange={(value) => setDoc((current) => { const next = { ...current }; if (value) next.default_profile = value; else delete next.default_profile; return next; })}
            options={[{ value: '', label: 'Прежний путь (legacy): ничего не менять' }, ...profileNames.map((name) => ({ value: name, label: name }))]}
            hint="Профиль по умолчанию получают все запросы, которых не поймало ни одно правило. Служебные запросы под защитой его не получают" />
        </div>
      </section>
    </div>
  );
}
