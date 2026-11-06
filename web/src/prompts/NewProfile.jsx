import { useEffect, useRef, useState } from 'react';
import { Button } from '../ui.jsx';
import { Line } from './controls.jsx';
import { STARTERS, profileNameError } from './draft.js';

/** Name and starting point of a new profile; the caller adds it to the draft. `pattern` is the relay's own name rule, when it published one. */
export default function NewProfile({ taken, onCreate, onCancel, pattern, suggested = 'mine', title = 'Новый профиль' }) {
  const [name, setName] = useState(suggested);
  const [starter, setStarter] = useState('same');
  const input = useRef(null);
  const error = profileNameError(name, taken, pattern ?? undefined);
  // the form opens on its name, selected: typing replaces the suggestion
  useEffect(() => { input.current?.focus(); input.current?.select(); }, []);
  return (
    <form className="panel p-new-profile" onSubmit={(event) => { event.preventDefault(); if (!error) onCreate(name, starter); }}>
      <h2>{title}</h2>
      <Line label="Имя профиля" value={name} onChange={setName} inputRef={input} hint="Латиница, цифры, точка, дефис и подчёркивание. Имя станет суффиксом модели: <модель>@имя" />
      {error && name !== suggested && <p className="p-error">{error}</p>}
      <fieldset className="p-starters">
        <legend>С чего начать</legend>
        {STARTERS.map((item) => (
          <label key={item.id} className={`prompt-mode ${starter === item.id ? 'selected' : ''}`}>
            <input type="radio" name="starter" value={item.id} checked={starter === item.id} onChange={() => setStarter(item.id)} />
            <span><strong>{item.label}</strong><small>{item.hint}</small></span>
          </label>
        ))}
      </fieldset>
      <div className="form-actions">
        {onCancel && <Button onClick={onCancel}>Отмена</Button>}
        <Button type="submit" tone="primary" disabled={!!error}>Создать профиль</Button>
      </div>
    </form>
  );
}
