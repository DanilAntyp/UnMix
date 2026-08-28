import { useEffect, useRef, useState } from 'react'
import { postForm, postJSON } from '../api'
import { DropZone, Status, MediaCard, PanelHead, IconBlend, TrackFacts } from '../ui'
import { LiquidMetalButton } from '../LiquidMetalButton'

function camelotCompatible(a, b) {
  if (!a || !b) return null
  if (a === b) return true
  const na = parseInt(a), la = a.slice(-1)
  const nb = parseInt(b), lb = b.slice(-1)
  if (na === nb) return true                                  // relative major/minor
  if (la === lb && ((na - nb + 12) % 12 === 1 || (nb - na + 12) % 12 === 1)) return true
  return false
}

function SongSlot({ label, hint, value, onChange, files, refresh }) {
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
        <option value="">choose a downloaded song…</option>
        {files.map(f => <option key={f.file} value={f.file}>{f.name}</option>)}
      </select>
      <DropZone onFile={upload} icon={<IconBlend width={22} height={22} color="#9a9aa8" />}
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

export function MashupView() {
  const [files, setFiles] = useState([])
  const [a, setA] = useState(null)
  const [b, setB] = useState(null)
  const [aF, setAF] = useState(null)
  const [bF, setBF] = useState(null)
  const [semis, setSemis] = useState(0)
  const [offset, setOffset] = useState('')
  const [status, setStatus] = useState(null)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const pollRef = useRef(null)

  const refresh = () => fetch('/files').then(r => r.json()).then(d => setFiles(d.files || [])).catch(() => {})
  useEffect(() => { refresh() }, [])
  useEffect(() => () => clearTimeout(pollRef.current), [])

  useEffect(() => {
    setAF(null)
    if (a) postJSON('/analyze', { server_file: decodeURI(a) }).then(setAF).catch(() => {})
  }, [a])
  useEffect(() => {
    setBF(null)
    if (b) postJSON('/analyze', { server_file: decodeURI(b) }).then(setBF).catch(() => {})
  }, [b])

  let compat = null
  if (aF?.bpm && bF?.bpm) {
    let ratio = bF.bpm / aF.bpm
    while (ratio > 1.5) ratio /= 2
    while (ratio < 0.667) ratio *= 2
    const pct = ((ratio - 1) * 100).toFixed(1)
    const keys = camelotCompatible(aF.camelot, bF.camelot)
    compat = { pct, keys }
  }

  async function start() {
    if (!a || !b || busy) return
    setBusy(true); setResult(null)
    setStatus({ busy: true, text: 'Starting…' })
    try {
      const data = await postJSON('/mashup/start', {
        a_file: decodeURI(a), b_file: decodeURI(b),
        semitones: semis, offset: parseFloat(offset) || 0,
        acapella_gain: 1.15, instrumental_gain: 0.9,
      })
      poll(data.job)
    } catch (e) {
      setStatus({ text: 'Error: ' + e.message, error: true })
      setBusy(false)
    }
  }

  async function poll(id) {
    try {
      const j = await (await fetch('/mashup/status/' + id)).json()
      if (j.error) throw new Error(j.error)
      if (!j.done) {
        setStatus({ busy: true, text: j.stage, pct: j.pct })
        pollRef.current = setTimeout(() => poll(id), 1500)
        return
      }
      setStatus({ text: `Done! Acapella stretched ×${j.stretch}` })
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
        tile="tile-rose" icon={<IconBlend />}
        title="Mashup Maker" sub="Vocals from song A over the instrumental of song B, tempo-matched"
      />
      <div className="slots">
        <SongSlot label="Song A" hint="vocals come from here" value={a} onChange={setA}
          files={files} refresh={refresh} />
        <SongSlot label="Song B" hint="instrumental comes from here" value={b} onChange={setB}
          files={files} refresh={refresh} />
      </div>

      {compat && (
        <div className={'compat' + (compat.keys === false ? ' warn' : '')}>
          Tempo: acapella will stretch {compat.pct > 0 ? '+' : ''}{compat.pct}% to match B
          {compat.keys != null && (
            <> · Keys ({aF.camelot} → {bF.camelot}): {compat.keys
              ? 'compatible ✓'
              : 'clash — try shifting the acapella a few semitones'}</>
          )}
        </div>
      )}

      <div className="setting">
        <label>Shift acapella:</label>
        <select value={semis} onChange={e => setSemis(parseInt(e.target.value))}>
          {[-4, -3, -2, -1, 0, 1, 2, 3, 4].map(s => (
            <option key={s} value={s}>{s === 0 ? 'no pitch shift' : `${s > 0 ? '+' : ''}${s} semitones`}</option>
          ))}
        </select>
        <label>Delay vocals:</label>
        <input type="text" style={{ maxWidth: 110 }} placeholder="0 s" value={offset}
          onChange={e => setOffset(e.target.value)} />
      </div>

      {a && b && (
        <div style={{ marginTop: 20, display: 'flex', justifyContent: 'center' }}>
          <LiquidMetalButton label="Create mashup" width={180} onClick={start} disabled={busy} />
        </div>
      )}
      <Status {...(status || {})} />
      {result && (
        <div className="results">
          <MediaCard title={decodeURIComponent(result.file.split('/').pop())} url={result.file} accent="#e8e8e8" />
        </div>
      )}
    </div>
  )
}
