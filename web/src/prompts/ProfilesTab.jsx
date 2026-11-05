import { useRef, useState } from 'react';
import { Plus } from 'lucide-react';
import { Button, Tag } from '../ui.jsx';
import { Notice } from './controls.jsx';
import { addProfile, profileUsage, profilesOf, removeProfile, renameProfile, starterProfile, uniqueName } from './draft.js';
import { useFocusLater } from './hooks.js';
import LivePreview from './LivePreview.jsx';
import NewProfile from './NewProfile.jsx';
import ProfileEditor from './ProfileEditor.jsx';

export default function ProfilesTab({ apiKey, doc, setDoc, builtin, selected, select, onAddRule, live, onUnauthorized }) {
  const [creating, setCreating] = useState(false);
  const root = useRef(null);
  const focusLater = useFocusLater();
  const names = Object.keys(profilesOf(doc));
  const builtinNames = builtin.profiles.length ? builtin.profiles : ['passthrough'];
  const current = names.includes(selected) ? selected : builtinNames.includes(selected) ? selected : names[0] || null;
  const textNames = Object.keys(doc.texts || {});
  const isBuiltin = !!current && builtinNames.includes(current);

  // The form of a profile is a new element for every profile, so what had the focus is gone after a create, a copy, a
  // rename or a removal: the focus goes to the chip of the profile now in view.
  const toChip = () => root.current?.querySelector('.p-profile-bar [aria-current="true"]');
  const create = (name, starter) => {
    setDoc((d) => addProfile(d, name, starterProfile(starter)));
    select(name);
    setCreating(false);
    focusLater(toChip);
  };
  const stopCreating = () => {
    setCreating(false);
    focusLater(() => root.current?.querySelector('[data-act="new-profile"]'));
  };
  const duplicate = () => {
    const name = uniqueName(`${current}-copy`, names);
    setDoc((d) => addProfile(d, name, structuredClone(d.profiles[current])));
    select(name);
    focusLater(toChip);
  };
  const remove = () => {
    if (!window.confirm(`Убрать профиль ${current} из черновика? Изменения вступят в силу после сохранения`)) return;
    setDoc((d) => removeProfile(d, current));
    select(null);
    focusLater(toChip);
  };
  const rename = (next) => {
    setDoc((d) => renameProfile(d, current, next));
    select(next);
    focusLater(toChip);
  };

  return (
    <div className="p-profiles" ref={root}>
      <div className="p-profile-bar" role="group" aria-label="Профили">
        {[...builtinNames, ...names].map((name) => (
          <button type="button" key={name} className={`chip mono ${name === current ? 'on' : ''}`} aria-current={name === current ? 'true' : undefined} title={name}
            onClick={() => { select(name); setCreating(false); }}>
            <span className="p-chip-name">{name}</span>{builtinNames.includes(name) && <>{' '}<Tag>встроенный</Tag></>}
          </button>
        ))}
        <Button icon={Plus} data-act="new-profile" onClick={() => setCreating(true)} disabled={creating}>Новый профиль</Button>
      </div>
      {creating && <NewProfile taken={[...names, ...builtinNames]} pattern={builtin.profileName} suggested={uniqueName('mine', names)} onCreate={create} onCancel={stopCreating} />}
      {!creating && isBuiltin && (
        <section className="panel">
          <h2 className="mono">{current}</h2>
          <p className="p-hint">Встроенный профиль: head — <code>{'{{client_system}}'}</code>, то есть system клиента без изменений. Его используют защитные правила для служебных запросов. Переопределить его нельзя</p>
        </section>
      )}
      {!creating && !isBuiltin && current && (
        <div className="p-profile-layout">
          <section className="panel p-profile-panel" aria-label={`Профиль ${current}`}>
            <ProfileEditor key={current} doc={doc} setDoc={setDoc} name={current} builtin={builtin} textNames={textNames}
              usage={profileUsage(doc, current)} onRename={rename} onDuplicate={duplicate} onDelete={remove} onAddRule={() => onAddRule(current)} />
          </section>
          <LivePreview apiKey={apiKey} doc={doc} profile={current} {...live} onUnauthorized={onUnauthorized} />
        </div>
      )}
      {!creating && !isBuiltin && !current && (
        <Notice tone="warn">Профилей пока нет. Создайте первый: он начнёт с system клиента без изменений</Notice>
      )}
    </div>
  );
}
