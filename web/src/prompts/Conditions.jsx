import { useState } from 'react';
import { X } from 'lucide-react';
import { IconBtn, Select, Tokens } from './controls.jsx';
import { fromValues, setCondition, valuesOf } from './conditions.js';

function BoolCondition({ spec, value, onChange, path }) {
  return (
    <Select label={spec.label} value={String(value)} onChange={(next) => onChange(next === 'true')} path={path}
      options={[{ value: 'true', label: 'да' }, { value: 'false', label: 'нет' }]} hint={spec.hint} />
  );
}

/** The conditions of one rule: all of them must hold (AND); several values of one condition are alternatives (OR). */
export default function Conditions({ value, onChange, specs, path }) {
  const conditions = value || {};
  const [pending, setPending] = useState([]);
  const known = new Map(specs.map((spec) => [spec.name, spec]));
  // A condition without values yet lives only here; a condition the screen does not know is kept as a plain list.
  const names = [...new Set([...Object.keys(conditions), ...pending])];
  const free = specs.filter((spec) => !names.includes(spec.name));
  const remove = (name) => {
    setPending((list) => list.filter((item) => item !== name));
    onChange(setCondition(conditions, name, undefined));
  };
  const add = (name) => {
    if (!name) return;
    if (known.get(name)?.kind === 'bool') onChange(setCondition(conditions, name, true));
    else setPending((list) => [...list, name]);
  };
  return (
    <div className="p-conditions">
      <p className="p-hint">Все условия должны выполняться сразу. Несколько значений одного условия — любое из них. Строки — маски: <code>*</code> и <code>?</code>, без учёта регистра</p>
      {!names.length && <p className="p-hint">Условий нет: правило сработает на любой запрос</p>}
      {names.map((name) => {
        const spec = known.get(name) || { name, label: name, kind: 'list' };
        const set = (next) => onChange(setCondition(conditions, name, next));
        return (
          <div className="p-condition" key={name}>
            {spec.kind === 'bool' || typeof conditions[name] === 'boolean' ? (
              <BoolCondition spec={spec} value={conditions[name] ?? true} onChange={set} path={`${path}.${name}`} />
            ) : (
              <Tokens label={spec.label} values={valuesOf(conditions[name])} onChange={(values) => set(fromValues(values))} suggestions={spec.values || []}
                placeholder={spec.values ? 'значение или маска' : 'значение или маска, например claude-*'} hint={spec.hint} path={`${path}.${name}`} />
            )}
            <IconBtn icon={X} label={`Убрать условие «${spec.label}»`} onClick={() => remove(name)} />
          </div>
        );
      })}
      {free.length > 0 && (
        <Select label="Добавить условие" value="" onChange={add}
          options={[{ value: '', label: 'Выберите условие' }, ...free.map((spec) => ({ value: spec.name, label: spec.label }))]} />
      )}
    </div>
  );
}
