import { useEffect, useRef, useState } from 'react'
import { postForm, postJSON } from '../api'
import { DropZone, Status, MediaCard, PanelHead, TrackFacts, IconNote } from '../ui'
import { LiquidMetalButton } from '../LiquidMetalButton'
import { fmtTime } from '../Waveform'

const STYLES = [
  { id: 'automix', name: 'AutoMix', desc: 'Apple Music-style smooth blend: phrase-aligned, intro-skipping, loudness-matched (recommended)' },
  { id: 'neural', name: 'Neural stem swap', desc: 'vocals leave first, bass swaps on the drop, drums hand over last' },
  { id: 'bassswap', name: 'Bass swap', desc: 'everything blends, the basslines hard-swap halfway' },
  { id: 'crossfade', name: 'Crossfade', desc: 'classic equal-power blend' },
  { id: 'filter', name: 'Filter sweep', desc: 'track A drains through a rising highpass' },
  { id: 'echo', name: 'Echo out', desc: 'A chops into echo tails, B drops in' },
  { id: 'cut', name: 'Cut', desc: 'clean switch on the beat' },
]

function DeckSlot({ label, hint, value, onChange, files, refresh }) {
  const [uploading, setUploading] = useState(false)
  async function upload(f) {
    setUploading(true)
    try {
      const fd = new FormData()
      fd.append('audio', f)
      const up = await postForm('/upload', fd)
      refresh()
      onChange(up.file)
    } catch {} finally { setUploading(false) }
  }
  return (
    <div className="slot">
      <div className="slot-label">{label} <span>{hint}</span></div>
      <select className="slot-select" value={value || ''} onChange={e => onChange(e.target.value || null)}>
        <option value="">choose a downloaded track…</option>
        {files.map(f => <option key={f.file} value={f.file}>{f.name}</option>)}
      </select>
      <DropZone onFile={upload} icon={<IconNote width={22} height={22} color="#9a9aa8" />}
        hint={uploading ? 'uploading…' : 'or drop a new file'} accept="audio/*,.mp3,.wav,.flac,.m4a,.ogg,.aac" />
      {value && (
        <div className="slot-picked">
          ♪ {decodeURIComponent(value.split('/').pop())}
          <TrackFacts url={value} />
        </div>
      )}
    </div>
  )
}

export function DJView() {
  const [files, setFiles] = useState([])
  const [a, setA] = useState(null)
  const [b, setB] = useState(null)
  const [style, setStyle] = useState('automix')
  const [beats, setBeats] = useState(32)
  const [status, setStatus] = useState(null)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const pollRef = useRef(null)

  const refresh = () => fetch('/files').then(r => r.json()).then(d => setFiles(d.files || [])).catch(() => {})
  useEffect(() => { refresh() }, [])
  useEffect(() => () => clearTimeout(pollRef.current), [])

  async function start() {
    if (!a || !b || busy) return
    setBusy(true); setResult(null)
    setStatus({ busy: true, text: 'Starting…' })
    try {
      const data = await postJSON('/dj/start', {
        a_file: decodeURI(a), b_file: decodeURI(b), style, beats,
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
        pollRef.current = setTimeout(() => poll(id), 1200)
        return
      }
      setStatus({
        text: `Done! Transition at ${fmtTime(j.transition_at)}` +
          (j.b_skip > 0.5 ? ` · B enters from ${fmtTime(j.b_skip)} (intro skipped)` : '') +
          (Math.abs(j.stretch - 1) > 0.005 ? ` · B stretched ×${j.stretch}` : '') +
          (j.fallback ? ` · fell back to ${j.fallback}` : ''),
      })
      setResult(j)
      setBusy(false)
    } catch (e) {
      setStatus({ text: 'Error: ' + e.message, error: true })
      setBusy(false)
    }
  }

  const styleInfo = STYLES.find(s => s.id === style)

  return (
    <div className="glass metal-scope">
      <PanelHead
        tile="tile-rose" icon={<IconNote />}
        title="DJ Transition" sub="Join two tracks with a real DJ-style transition, beat-matched"
      />
      <div className="slots">
        <DeckSlot label="Deck A" hint="plays first" value={a} onChange={setA}
          files={files} refresh={refresh} />
        <DeckSlot label="Deck B" hint="comes in after" value={b} onChange={setB}
          files={files} refresh={refresh} />
      </div>

      <div className="setting">
        <label>Transition:</label>
        <select value={style} onChange={e => setStyle(e.target.value)}>
          {STYLES.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        <label>Length:</label>
        <select value={beats} onChange={e => setBeats(parseInt(e.target.value))} style={{ maxWidth: 130 }}>
          {[4, 8, 16, 32, 64].map(n => <option key={n} value={n}>{n} beats</option>)}
        </select>
      </div>
      {styleInfo && <div className="wave-hint" style={{ marginTop: 8 }}>{styleInfo.desc}</div>}

      {a && b && (
        <div style={{ marginTop: 20, display: 'flex', justifyContent: 'center' }}>
          <LiquidMetalButton label="Mix the tracks" width={180} onClick={start} disabled={busy} />
        </div>
      )}
      <Status {...(status || {})} />
      {result && (
        <div className="results">
          <MediaCard title={decodeURIComponent(result.file.split('/').pop())}
            url={result.file} accent="#e8e8e8" />
        </div>
      )}
    </div>
  )
}
