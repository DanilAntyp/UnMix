/** A glass card. `accent`/`accent2` tint its glow, buttons and focus rings. */
export default function Panel({ mark, title, desc, accent, accent2, children }) {
  return (
    <section className="panel" style={{ '--accent': accent, '--accent-2': accent2 }}>
      <div className="panel-head">
        <div className="panel-mark" aria-hidden="true">{mark}</div>
        <h2>{title}</h2>
      </div>
      <p className="desc">{desc}</p>
      {children}
    </section>
  )
}
