export function Field({ label, children }) {
  return (
    <div className="field">
      <span className="label">{label}</span>
      {children}
    </div>
  )
}

export function Select({ value, onChange, options }) {
  return (
    <select value={value} onChange={e => onChange(e.target.value)}>
      {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  )
}

export function Check({ checked, onChange, children, sub }) {
  return (
    <label className={'check' + (checked ? ' on' : '')}>
      <input type="checkbox" checked={checked} onChange={e => onChange(e.target.checked)} />
      <span className="box" aria-hidden="true">
        <svg width="12" height="10" viewBox="0 0 12 10" fill="none">
          <path d="M1 5l3.4 3.4L11 1.6" stroke="#0a0710" strokeWidth="2.2"
                strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </span>
      <span>{children}{sub && <span className="sub"> {sub}</span>}</span>
    </label>
  )
}
