import { useEffect, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, Plus, Trash } from 'lucide-react';
import { Button } from '../ui.jsx';
import { Area, Check, IconBtn, Line, Select } from './controls.jsx';
import { idOf, insertAt, isObject, moveItem, removeAt, updateAt } from './draft.js';
import { useFocusLater } from './hooks.js';
import { SCOPES, describeEdit, flagsOf, joinFlags, newEdit, opOf, setEditField, switchOp } from './ops.js';

const FLAG_LABELS = [['i', 'i · без учёта регистра'], ['m', 'm · ^ и $ на каждой строке'], ['s', 's · точка берёт и перевод строки']];

function Flags({ value, onChange }) {
  const flags = flagsOf(value);
  return (
    <fieldset className="p-flags">
      <legend>Флаги</legend>
      {FLAG_LABELS.map(([flag, label]) => (
        <label className="check" key={flag}>
          <input type="checkbox" checked={flags.has(flag)}
            onChange={(event) => { const next = new Set(flags); if (event.target.checked) next.add(flag); else next.delete(flag); onChange(joinFlags(next)); }} />
          <span>{label}</span>
        </label>
      ))}
    </fieldset>
  );
}

// One heading per line; the box keeps what is typed, the edit keeps the cleaned list.
function Lines({ label, value, onChange, hint, placeholder, path, required }) {
  const list = Array.isArray(value) ? value : [];
  const parse = (text) => text.split('\n').map((line) => line.trim()).filter(Boolean);
  const [text, setText] = useState(() => list.join('\n'));
  useEffect(() => {
    const typed = parse(text);
    if (typed.length !== list.length || typed.some((line, i) => line !== list[i])) setText(list.join('\n'));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  return (
    <Area label={label} value={text} rows={3} hint={hint} placeholder={placeholder} path={path} required={required}
      onChange={(next) => { setText(next); onChange(parse(next)); }} />
  );
}

function FromFile({ label, value }) {
  return (
    <div className="p-field">
      <span className="p-label">{label}</span>
      <p className="p-filechip mono">из файла: {value.file}</p>
    </div>
  );
}

function Field({ field, edit, onChange, path }) {
  const value = edit[field.key];
  if (isObject(value) && value.file) return <FromFile label={field.label} value={value} />;
  const set = (next) => onChange(setEditField(edit, field.key, next));
  const common = { label: field.label, hint: field.hint, path: `${path}.${field.key}`, required: field.required };
  if (field.kind === 'bool') return <Check label={field.label} checked={value === true} onChange={(next) => set(next || undefined)} path={common.path} />;
  if (field.kind === 'flags') return <Flags value={value} onChange={set} />;
  if (field.kind === 'lines') return <Lines {...common} value={value} placeholder={field.placeholder} onChange={set} />;
  if (field.kind === 'area') return <Area {...common} value={value} rows={3} placeholder={field.placeholder} onChange={set} />;
  return <Line {...common} value={value} placeholder={field.placeholder} mono={field.mono !== false} onChange={set} />;
}

function EditRow({ scope, edit, index, count, path, ops, onChange, onMove, onRemove }) {
  const specs = SCOPES[scope].ops;
  const op = opOf(scope, edit);
  const spec = specs[op];
  const choices = [...new Set([...ops, ...(op && !ops.includes(op) ? [op] : [])])];
  const rowPath = `${path}.${index}`;
  return (
    <li className="p-edit">
      <div className="p-edit-head">
        <span className="p-edit-no num" aria-hidden="true">{index + 1}</span>
        <Select label={`Операция правки ${index + 1}`} labelHidden value={op || ''} path={`${rowPath}.op`}
          options={choices.map((name) => ({ value: name, label: specs[name]?.label || name }))}
          onChange={(next) => onChange(switchOp(scope, edit, next))} />
        <div className="p-edit-tools">
          <IconBtn icon={ArrowUp} label={`Поднять правку ${index + 1}`} disabled={index === 0} data-act="up" onClick={() => onMove(index, index - 1)} />
          <IconBtn icon={ArrowDown} label={`Опустить правку ${index + 1}`} disabled={index === count - 1} data-act="down" onClick={() => onMove(index, index + 1)} />
          <IconBtn icon={Trash} tone="danger" label={`Удалить правку ${index + 1}`} data-act="remove" onClick={() => onRemove(index)} />
        </div>
      </div>
      {scope === 'tools' && (
        <Line label="Инструмент" value={edit.tool} onChange={(tool) => onChange(setEditField(edit, 'tool', tool))} path={`${rowPath}.tool`} required
          placeholder="bash, edit, mcp_*" hint="Имя или маска: * и ?" />
      )}
      {spec ? (
        <>
          {spec.hint && <p className="p-hint">{spec.hint}</p>}
          <div className="p-edit-fields">
            {spec.fields.map((field) => <Field key={field.key} field={field} edit={edit} path={rowPath} onChange={onChange} />)}
          </div>
        </>
      ) : (
        <p className="p-hint mono">Операция «{String(op)}» неизвестна экрану и сохранится как есть: {describeEdit(scope, edit)}</p>
      )}
    </li>
  );
}

/** An ordered list of edits for one scope of a profile; the relay applies them top to bottom. */
export default function EditList({ scope, edits, onChange, path, ops, ordered = true }) {
  const specs = SCOPES[scope].ops;
  const offered = ops.filter((op) => specs[op]);
  const [adding, setAdding] = useState(offered[0] || '');
  const kind = SCOPES[scope].noun;
  const root = useRef(null);
  const focusLater = useFocusLater();
  const rows = () => [...(root.current?.querySelectorAll('.p-edit-list > .p-edit') || [])];
  const add = () => {
    onChange(insertAt(edits, edits.length, newEdit(scope, offered.includes(adding) ? adding : offered[0])));
    // a new edit is for typing into: the focus goes to its first field, or to its operation when it has none
    focusLater(() => {
      const row = rows().at(-1);
      return row?.querySelector('input:not([type="checkbox"]):not([type="radio"]), textarea') || row?.querySelector('select');
    });
  };
  const move = (from, to) => {
    onChange(moveItem(edits, from, to));
    // the row moved in the list: the focus follows the button that was pressed, or the other way when it reached the end
    focusLater(() => {
      const row = rows()[to];
      return row?.querySelector(`[data-act="${to < from ? 'up' : 'down'}"]:not(:disabled)`) || row?.querySelector('[data-act="up"]:not(:disabled), [data-act="down"]:not(:disabled)');
    });
  };
  const remove = (at) => {
    onChange(removeAt(edits, at));
    // the pressed button is gone: the focus goes to the edit that took its place, or to the form that adds one
    focusLater(() => {
      const list = rows();
      return list[Math.min(at, list.length - 1)]?.querySelector('[data-act="remove"]') || root.current?.querySelector('.p-edit-add button');
    });
  };
  return (
    <div className="p-edits" ref={root}>
      {!edits.length && <p className="p-hint">Правок пока нет</p>}
      {edits.length > 0 && (
        <ol className="p-edit-list" aria-label={ordered ? `${kind}, по порядку применения` : kind}>
          {edits.map((edit, index) => (
            <EditRow key={idOf(edit)} scope={scope} edit={edit} index={index} count={edits.length} path={path} ops={offered}
              onChange={(next) => onChange(updateAt(edits, index, () => next))} onMove={move} onRemove={remove} />
          ))}
        </ol>
      )}
      {offered.length > 0 && (
        <div className="p-edit-add">
          <Select label="Новая правка" value={offered.includes(adding) ? adding : offered[0]} onChange={setAdding}
            options={offered.map((name) => ({ value: name, label: specs[name]?.label || name }))} />
          <Button icon={Plus} onClick={add}>Добавить правку</Button>
        </div>
      )}
    </div>
  );
}
