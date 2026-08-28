import { useEffect, useRef, useState } from 'react'
import { postForm, postJSON } from '../api'
import { DropZone, Status, MediaCard, PanelHead, TrackFacts, IconNote } from '../ui'
import { LiquidMetalButton } from '../LiquidMetalButton'
import { Waveform, fmtTime } from '../Waveform'
import { TransitionLane } from '../TransitionLane'

const STYLES = [
  { id: 'automix', name: 'AutoMix', desc: 'smooth Apple Music-style blend — best for pop, house, anything melodic' },
  { id: 'acapella', name: 'Acapella bridge', desc: "A's vocal goes naked, then B's beat drops underneath it — check the keys match" },
  { id: 'tapestop', name: 'Tape stop', desc: 'A powers down like a turntable, B slams in — for hard rap / trap' },
  { id: 'looproll', name: 'Loop roll', desc: 'last bar stutters faster and faster into the drop' },
  { id: 'backspin', name: 'Backspin', desc: 'rewind spin, then B drops — hip-hop radio classic' },
  { id: 'riser', name: 'Riser', desc: 'noise sweep builds over A, B drops on the peak — EDM style' },
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
  const [previews, setPreviews] = useState(null)
  const [busy, setBusy] = useState(false)
  const [inspect, setInspect] = useState(null)
  const [cutSec, setCutSec] = useState(null)
  const [bStartSec, setBStartSec] = useState(null)
  const pollRef = useRef(null)

  const refresh = () => fetch('/files').then(r => r.json()).then(d => setFiles(d.files || [])).catch(() => {})
  useEffect(() => { refresh() }, [])
  useEffect(() => () => clearTimeout(pollRef.current), [])

  useEffect(() => {
    setInspect(null); setCutSec(null); setBStartSec(null)
    if (!a || !b) return
    let cancelled = false
    setStatus({ busy: true, text: 'Analyzing both tracks…' })
    postJSON('/dj/inspect', { a_file: decodeURI(a), b_file: decodeURI(b), beats })
      .then(d => {
        if (cancelled) return
        setInspect(d)
        setCutSec(d.a.cut)
        setBStartSec(d.b.b_start)
        setStatus(null)
      })
      .catch(e => { if (!cancelled) setStatus({ text: 'Error: ' + e.message, error: true }) })
    return () => { cancelled = true }
  }, [a, b, beats])

  async function start(preview = false) {
    if (!a || !b || busy) return
    setBusy(true); setResult(null); setPreviews(null)
    setStatus({ busy: true, text: 'Starting…' })
    try {
      const body = { a_file: decodeURI(a), b_file: decodeURI(b), style, beats }
      if (preview) body.preview = true
      if (cutSec != null) body.cut = cutSec
      if (bStartSec != null) body.b_start = bStartSec
      const data = await postJSON('/dj/start', body)
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
      if (j.previews) {
        setStatus({ text: j.stage })
        setPreviews(j.previews)
        setBusy(false)
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

      {inspect && (
        <div className="editor glass-soft">
          <TransitionLane
            src={a} info={inspect.a} marker={cutSec} onMarker={setCutSec}
            label="Deck A — exit point"
            note={cutSec === inspect.a.cut
              ? `auto: ${inspect.a.cut_reason || 'proposed'}`
              : 'manual — drag to adjust'} />
          {cutSec !== inspect.a.cut && (
            <button className="chip tiny lane-reset" onClick={() => setCutSec(inspect.a.cut)}>
              reset to auto
            </button>
          )}
          <TransitionLane
            src={b} info={inspect.b} marker={bStartSec} onMarker={setBStartSec}
            label="Deck B — entry point"
            note={bStartSec === inspect.b.b_start ? 'auto: first full-energy section' : 'manual — drag to adjust'} />
          {bStartSec !== inspect.b.b_start && (
            <button className="chip tiny lane-reset" onClick={() => setBStartSec(inspect.b.b_start)}>
              reset to auto
            </button>
          )}
          <div className="wave-hint" style={{ marginTop: 6 }}>
            brighter waveform = higher-energy section · vertical lines = detected section boundaries ·
            markers snap to bars
          </div>
        </div>
      )}

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
        <div style={{ marginTop: 20, display: 'flex', justifyContent: 'center', gap: 14, alignItems: 'center' }}>
          <LiquidMetalButton label="Mix the tracks" width={180} onClick={() => start(false)} disabled={busy} />
          <button className="chip" disabled={busy} onClick={() => start(true)}>
            Preview all styles (30s clips)
          </button>
        </div>
      )}
      <Status {...(status || {})} />
      {previews && (
        <div className="results">
          {previews.map(p => p.error ? (
            <div key={p.style} className="media-card glass-soft">
              <div className="media-row"><span className="media-name">{p.style}</span>
                <span style={{ color: '#ff6961', fontSize: '0.82rem' }}>{p.error}</span></div>
            </div>
          ) : (
            <div key={p.style} className="media-card glass-soft">
              <div className="media-row">
                <span className="media-name">{STYLES.find(s => s.id === p.style)?.name || p.style}</span>
                <button className="chip" onClick={() => setStyle(p.style)}>Use this style</button>
              </div>
              <Waveform src={encodeURI(p.file)} height={48} accent="#e8e8e8" />
            </div>
          ))}
        </div>
      )}
      {result && (
        <div className="results">
          <MediaCard title={decodeURIComponent(result.file.split('/').pop())}
            url={result.file} accent="#e8e8e8" />
        </div>
      )}
    </div>
  )
}
