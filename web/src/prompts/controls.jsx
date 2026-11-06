import { createContext, useContext, useId, useRef, useState } from 'react';
import { ChevronRight, X } from 'lucide-react';
import { fmtInt } from '../format.js';
import { errorHits } from './draft.js';
import { utf8Bytes } from './template.js';

// The error a failed save blames, {message, param}: a control shows it when the path is its own or inside it.
export const ErrorContext = createContext(null);

export function useFieldError(path) {
  const error = useContext(ErrorContext);
  return error && path && errorHits(error.param, path) ? error.message : '';
}

function Message({ id, error, hint, alert }) {
  return (
    <>
      {error && <p className="p-error" id={`${id}-error`} role={alert ? 'alert' : undefined}>{error}</p>}
      {hint && <p className="p-hint" id={`${id}-hint`}>{hint}</p>}
    </>
  );
}

const describedBy = (id, error, hint) => [hint && `${id}-hint`, error && `${id}-error`].filter(Boolean).join(' ') || undefined;

/** A label, a control and what is wrong with it; `path` lets a failed save point at this field. */
export function Line({ label, value, onChange, path, hint, placeholder, mono = true, disabled, required, type = 'text', inputRef, className = '' }) {
  const id = useId();
  const blamed = useFieldError(path);
  const error = blamed || (required && !String(value ?? '').trim() ? 'Заполните поле' : '');
  return (
    <div className={`p-field ${className}`}>
      <label htmlFor={id}>{label}</label>
      <input id={id} ref={inputRef} type={type} className={`p-input${mono ? ' mono' : ''}`} value={value ?? ''} data-path={path} placeholder={placeholder}
        disabled={disabled} spellCheck={false} autoComplete="off" aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(id, error, hint)} onChange={(event) => onChange(event.target.value)} />
      <Message id={id} error={error} hint={hint} alert={!!blamed} />
    </div>
  );
}

/** A text box with the counter the relay judges by: UTF-8 bytes against its limit. */
export function Area({ label, value, onChange, path, hint, placeholder, rows = 6, limit, mono = true, disabled, readOnly, required, areaRef, className = '', labelHidden }) {
  const id = useId();
  const blamed = useFieldError(path);
  const text = value ?? '';
  const bytes = limit ? utf8Bytes(text) : 0;
  const over = limit > 0 && bytes > limit;
  const error = blamed || (over ? `Текст больше ${fmtInt(limit)} байт: relay его не примет` : '') || (required && !text.trim() ? 'Заполните поле' : '');
  return (
    <div className={`p-field ${className}`}>
      <label htmlFor={id} className={labelHidden ? 'p-sr' : undefined}>{label}</label>
      <textarea id={id} ref={areaRef} className={`p-area${mono ? ' mono' : ''}`} rows={rows} value={text} data-path={path} placeholder={placeholder}
        disabled={disabled} readOnly={readOnly} spellCheck={false} aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(id, error, hint)} onChange={(event) => onChange(event.target.value)} />
      <div className="p-field-foot">
        <div><Message id={id} error={error} hint={hint} alert={!!blamed} /></div>
        {limit > 0 && (
          <span className={`mono p-count${over ? ' neg-text' : ''}`}>{fmtInt(text.length)} симв. · {fmtInt(bytes)} из {fmtInt(limit)} байт</span>
        )}
      </div>
    </div>
  );
}

export function Select({ label, value, onChange, options, path, hint, disabled, className = '', labelHidden }) {
  const id = useId();
  const blamed = useFieldError(path);
  const error = blamed;
  return (
    <div className={`p-field ${className}`}>
      <label htmlFor={id} className={labelHidden ? 'p-sr' : undefined}>{label}</label>
      <select id={id} className="p-select" value={value} data-path={path} disabled={disabled} aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(id, error, hint)} onChange={(event) => onChange(event.target.value)}>
        {options.map((option) => <option key={option.value} value={option.value} disabled={option.disabled}>{option.label}</option>)}
      </select>
      <Message id={id} error={error} hint={hint} alert={!!blamed} />
    </div>
  );
}

export function Check({ label, checked, onChange, hint, disabled, path }) {
  const id = useId();
  const blamed = useFieldError(path);
  const error = blamed;
  return (
    <div className="p-field p-check">
      <label className="check" htmlFor={id}>
        <input id={id} type="checkbox" checked={!!checked} disabled={disabled} data-path={path} aria-describedby={describedBy(id, error, hint)}
          onChange={(event) => onChange(event.target.checked)} />
        <span>{label}</span>
      </label>
      <Message id={id} error={error} hint={hint} alert={!!blamed} />
    </div>
  );
}

export function IconBtn({ icon: Icon, label, onClick, disabled, tone, ...rest }) {
  return (
    <button type="button" className={`icon-button${tone ? ` tone-${tone}` : ''}`} aria-label={label} title={label} disabled={disabled} onClick={onClick} {...rest}>
      <Icon size={16} strokeWidth={1.5} aria-hidden="true" />
    </button>
  );
}

/** Values typed or picked one by one; the stored value is a string, or a list when there are several (OR). */
export function Tokens({ label, values, onChange, suggestions = [], placeholder, hint, path }) {
  const id = useId();
  const [draft, setDraft] = useState('');
  const input = useRef(null);
  const blamed = useFieldError(path);
  const error = blamed;
  const add = (raw) => {
    const fresh = String(raw).split(/[,\n]/).map((item) => item.trim()).filter((item) => item && !values.includes(item));
    setDraft('');
    if (fresh.length) onChange([...new Set([...values, ...fresh])]);
  };
  const free = suggestions.filter((item) => !values.includes(item));
  return (
    <div className="p-field">
      <span className="p-label" id={`${id}-label`}>{label}</span>
      <div className="p-tokens" role="group" aria-labelledby={`${id}-label`} data-path={path}>
        {values.map((value) => (
          <span className="p-token" key={value}>
            <code>{value}</code>
            <button type="button" aria-label={`Убрать значение ${value}`} onClick={() => { onChange(values.filter((item) => item !== value)); input.current?.focus(); }}>
              <X size={12} strokeWidth={1.5} aria-hidden="true" />
            </button>
          </span>
        ))}
        <input ref={input} className="mono" value={draft} placeholder={values.length ? '' : placeholder} aria-label={`${label}: добавить значение`} spellCheck={false}
          autoComplete="off" aria-describedby={describedBy(id, error, hint)} onChange={(event) => setDraft(event.target.value)} onBlur={() => add(draft)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' || event.key === ',') { event.preventDefault(); add(draft); }
            else if (event.key === 'Backspace' && !draft && values.length) onChange(values.slice(0, -1));
          }} />
      </div>
      {free.length > 0 && (
        <div className="p-suggest" role="group" aria-label={`${label}: готовые значения`}>
          {free.map((item) => <button type="button" key={item} className="chip" onClick={() => add(item)}>{item}</button>)}
        </div>
      )}
      <Message id={id} error={error} hint={hint} alert={!!blamed} />
    </div>
  );
}

/**
 * A native disclosure: keyboard and screen readers get it for free. Only the user's own click on the summary changes
 * it, so edits never fold it; `open` and `onOpenChange` let a parent decide the state until the user has chosen.
 */
export function Disclosure({ title, meta, defaultOpen = false, open: controlled, onOpenChange, children, id }) {
  const [inner, setInner] = useState(defaultOpen);
  const open = controlled ?? inner;
  return (
    <details className="p-disclosure" open={open} id={id}>
      <summary onClick={(event) => { event.preventDefault(); (onOpenChange || setInner)(!open); }}>
        <ChevronRight className="chev" size={16} strokeWidth={1.5} aria-hidden="true" />
        <span className="p-disclosure-title">{title}</span>
        {meta && <span className="p-disclosure-meta">{meta}</span>}
      </summary>
      <div className="p-disclosure-body">{children}</div>
    </details>
  );
}

export function Notice({ tone = 'warn', icon: Icon, children, role = 'status', className = '' }) {
  return (
    <div className={`banner tone-${tone} p-notice ${className}`} role={role}>
      {Icon && <Icon size={16} strokeWidth={1.5} aria-hidden="true" />}
      <div>{children}</div>
    </div>
  );
}
