/** Drifting colour fields + film grain behind everything. */
export default function Backdrop() {
  return (
    <>
      <div className="aurora" aria-hidden="true">
        <span className="a1" /><span className="a2" /><span className="a3" /><span className="a4" />
      </div>
      <div className="grain" aria-hidden="true" />
    </>
  )
}
