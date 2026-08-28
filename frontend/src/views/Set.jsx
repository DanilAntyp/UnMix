import { useEffect, useRef, useState } from 'react'
import { postJSON } from '../api'
import { Status, MediaCard, PanelHead, TrackFacts, IconNote } from '../ui'
import { LiquidMetalButton } from '../LiquidMetalButton'
import { fmtTime } from '../Waveform'

export function SetView() {
  const [files, setFiles] = useState([])
  const [picked, setPicked] = useState({})
  const [beats, setBeats] = useState(32)
  const [status, setStatus] = useState(null)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const pollRef = useRef(null)

  useEffect(() => {
    fetch('/files').then(r => r.json()).then(d => setFiles(d.files || [])).catch(() => {})
    return () => clearTimeout(pollRef.current)
  }, [])

  const chosen = files.filter(f => picked[f.file])

  async function start() {
    if (chosen.length < 2 || busy) return
    setBusy(true); setResult(null)
    setStatus({ busy: true, text: 'Starting…' })
    try {
      const data = await postJSON('/dj/set/start', {
        files: chosen.map(f => f.file), beats,
      })
      poll(data.job)
    } catch (e) {
      setStatus({ text: 'Error: ' + e.message, error: true })
      setBusy(false)
    }
  }

  async function poll(id) {
    try {
      const j = await (await fetch('/dj/status/' + id)).json()
      if (j.error) throw new Error(j.error)
      if (!j.done) {
        setStatus({ busy: true, text: j.stage, pct: j.pct })
        pollRef.current = setTimeout(() => poll(id), 1500)
        return
      }
      setStatus({ text: 'Done!' })
      setResult(j)
      setBusy(false)
    } catch (e) {
      setStatus({ text: 'Error: ' + e.message, error: true })
      setBusy(false)
    }
  }

  return (
    <div className="glass metal-scope">
      <PanelHead
        tile="tile-violet" icon={<IconNote />}
        title="Playlist AutoMix" sub="Pick tracks — get one continuous DJ set, ordered and blended for you"
      />
      <div className="setlist">
        {files.map(f => (
          <label key={f.file} className={'set-row' + (picked[f.file] ? ' on' : '')}>
            <input type="checkbox" checked={!!picked[f.file]}
              onChange={e => setPicked(p => ({ ...p, [f.file]: e.target.checked }))} />
            <span className="set-name">{f.name}</span>
            {picked[f.file] && <TrackFacts url={f.file} />}
          </label>
        ))}
        {!files.length && <div className="wave-hint">no tracks yet — download some first</div>}
      </div>

      <div className="dj-controls">
        <span className="wave-hint">{chosen.length} tracks selected</span>
        <select value={beats} onChange={e => setBeats(parseInt(e.target.value))}
          className="slot-select" style={{ maxWidth: 140, marginBottom: 0 }}>
          {[16, 32, 64].map(n => <option key={n} value={n}>{n}-beat blends</option>)}
        </select>
        {chosen.length >= 2 && (
          <LiquidMetalButton label="Render the set" width={170} onClick={start} disabled={busy} />
        )}
      </div>

      <Status {...(status || {})} />
      {result && (
        <div className="results">
          <MediaCard title={decodeURIComponent(result.file.split('/').pop())}
            url={result.file} accent="#e8e8e8" />
          <div className="media-card glass-soft">
            <div className="media-row"><span className="media-name">Tracklist</span></div>
            {result.tracklist.map((t, i) => (
              <div key={i} className="set-tl">
                <span className="set-tl-at">{fmtTime(t.at)}</span> {t.title}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
