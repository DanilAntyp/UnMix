/** One status line (spinner / error / done) plus an optional progress bar. */
export default function Status({ state }) {
  if (!state || !state.text) return null
  const { kind = 'busy', text, pct } = state
  return (
    <>
      <div className={'status' + (kind === 'error' ? ' err' : kind === 'done' ? ' ok' : '')}>
        {kind === 'busy' && <span className="spinner" />}
        {kind === 'done' && <span aria-hidden="true">&#10003;</span>}
        {kind === 'error' && <span aria-hidden="true">&#9888;</span>}
        <span>{text}</span>
      </div>
      {kind === 'busy' && pct != null && (
        <div className="bar"><i style={{ width: Math.max(2, Math.min(100, pct)) + '%' }} /></div>
      )}
    </>
  )
}
