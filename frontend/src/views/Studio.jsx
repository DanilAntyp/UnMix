import { useEffect, useRef, useState } from 'react'
import { postForm, postJSON, pollProgress, STEM_NAMES } from '../api'
import { DropZone, Status, MediaCard, PanelHead, IconSliders } from '../ui'
import { LiquidMetalButton } from '../LiquidMetalButton'
import { fmtTime } from '../Waveform'

const ORDER = ['vocals', 'drums', 'bass', 'other']

export function StudioView() {
  const [picked, setPicked] = useState(null)
  const [songName, setSongName] = useState('')
  const [stems, setStems] = useState(null)
  const [status, setStatus] = useState(null)
  const [busy, setBusy] = useState(false)

  function pick(f) {
    setPicked(f); setStems(null); setStatus(null)
  }

  async function split() {
    if (!picked || busy) return
    setBusy(true)
    setStatus({ busy: true, text: 'Uploading…' })
    let stop = () => {}
    try {
      const fd = new FormData()
      fd.append('audio', picked)
      const up = await postForm('/upload', fd)
      setSongName(up.name.replace(/\.[^.]+$/, ''))
      stop = pollProgress('/progress/sep', pct =>
        setStatus({ busy: true, text: 'Separating into stems…', pct }))
      const data = await postJSON('/separate', { server_file: up.file, remove: 'all', model: 'htdemucs' })
      stop()
      setStatus(null)
      setStems(data.all)
    } catch (e) {
      stop()
      setStatus({ text: 'Error: ' + e.message, error: true })
    } finally { setBusy(false) }
  }

  return (
    <div className="glass metal-scope">
      <PanelHead
        tile="tile-violet" icon={<IconSliders />}
        title="Stem Studio" sub="Split a song into tracks, remix the balance, export your mix"
      />
      {!stems && (
        <>
          <DropZone onFile={pick} icon={<IconSliders width={30} height={30} color="#9a9aa8" />}
            hint="mp3, wav, flac, m4a …" accept="audio/*,.mp3,.wav,.flac,.m4a,.ogg,.aac" />
          {picked && <div className="picked">♪ {picked.name}</div>}
          {picked && (
            <div style={{ marginTop: 18, display: 'flex', justifyContent: 'center' }}>
              <LiquidMetalButton label="Open in studio" width={180} onClick={split} disabled={busy} />
            </div>
          )}
        </>
      )}
      <Status {...(status || {})} />
      {stems && <StudioPlayer stems={stems} name={songName} onClose={() => { setStems(null); setPicked(null) }} />}
    </div>
  )
}

function StudioPlayer({ stems, name, onClose }) {
  const ctxRef = useRef(null)
  const buffersRef = useRef({})
  const gainNodesRef = useRef({})
  const sourcesRef = useRef(null)
  const startRef = useRef(0)
  const posRef = useRef(0)
  const rafRef = useRef(null)
  const stateRef = useRef({})

  const [ready, setReady] = useState(false)
  const [playing, setPlaying] = useState(false)
  const [pos, setPos] = useState(0)
  const [duration, setDuration] = useState(0)
  const [gains, setGains] = useState({ vocals: 1, drums: 1, bass: 1, other: 1 })
  const [muted, setMuted] = useState({})
  const [solo, setSolo] = useState(null)
  const [expStatus, setExpStatus] = useState(null)
  const [expResult, setExpResult] = useState(null)
  const [expBusy, setExpBusy] = useState(false)

  stateRef.current = { gains, muted, solo, duration }

  useEffect(() => {
    let cancelled = false
    const ctx = new (window.AudioContext || window.webkitAudioContext)()
    ctxRef.current = ctx
    ;(async () => {
      const entries = await Promise.all(ORDER.filter(k => stems[k]).map(async k => {
        const buf = await (await fetch(encodeURI(stems[k]))).arrayBuffer()
        return [k, await ctx.decodeAudioData(buf)]
      }))
      if (cancelled) return
      buffersRef.current = Object.fromEntries(entries)
      setDuration(Math.max(...entries.map(([, b]) => b.duration)))
      setReady(true)
    })()
    return () => {
      cancelled = true
      stopSources()
      ctx.close()
    }
  }, [stems])

  function effGain(k, s = stateRef.current) {
    if (s.solo) return k === s.solo ? s.gains[k] : 0
    return s.muted[k] ? 0 : s.gains[k]
  }

  function applyGains(next) {
    for (const k in gainNodesRef.current) {
      gainNodesRef.current[k].gain.value = effGain(k, next)
    }
  }

  function stopSources() {
    if (sourcesRef.current) {
      for (const s of sourcesRef.current) { try { s.stop() } catch {} }
      sourcesRef.current = null
    }
    cancelAnimationFrame(rafRef.current)
  }

  function play(at = posRef.current) {
    const ctx = ctxRef.current
    ctx.resume()
    stopSources()
    const sources = []
    gainNodesRef.current = {}
    for (const k in buffersRef.current) {
      const src = ctx.createBufferSource()
      src.buffer = buffersRef.current[k]
      const g = ctx.createGain()
      g.gain.value = effGain(k)
      src.connect(g)
      g.connect(ctx.destination)
      gainNodesRef.current[k] = g
      src.start(0, Math.min(at, src.buffer.duration))
      sources.push(src)
    }
    sourcesRef.current = sources
    startRef.current = ctx.currentTime - at
    setPlaying(true)
    const tick = () => {
      const p = ctx.currentTime - startRef.current
      posRef.current = p
      setPos(p)
      if (p >= stateRef.current.duration) { pause(0); return }
      rafRef.current = requestAnimationFrame(tick)
    }
    rafRef.current = requestAnimationFrame(tick)
  }

  function pause(at = null) {
    if (sourcesRef.current && at == null) {
      posRef.current = ctxRef.current.currentTime - startRef.current
    }
    stopSources()
    if (at != null) posRef.current = at
    setPos(posRef.current)
    setPlaying(false)
  }

  function seek(t) {
    posRef.current = t
    setPos(t)
    if (playing) play(t)
  }

  function setGain(k, v) {
    const gains2 = { ...gains, [k]: v }
    setGains(gains2)
    applyGains({ ...stateRef.current, gains: gains2 })
  }
  function toggleMute(k) {
    const m2 = { ...muted, [k]: !muted[k] }
    setMuted(m2)
    applyGains({ ...stateRef.current, muted: m2 })
  }
  function toggleSolo(k) {
    const s2 = solo === k ? null : k
    setSolo(s2)
    applyGains({ ...stateRef.current, solo: s2 })
  }

  async function exportMix() {
    setExpBusy(true); setExpResult(null)
    setExpStatus({ busy: true, text: 'Rendering your mix…' })
    try {
      const tracks = ORDER.filter(k => stems[k] && effGain(k) > 0)
        .map(k => ({ file: stems[k], gain: effGain(k) }))
      if (!tracks.length) throw new Error('everything is muted')
      const data = await postJSON('/studio/export', { name, tracks })
      setExpStatus({ text: 'Done!' })
      setExpResult(data)
    } catch (e) {
      setExpStatus({ text: 'Error: ' + e.message, error: true })
    } finally { setExpBusy(false) }
  }

  if (!ready) return <Status busy text="Loading stems into the studio…" />

  return (
    <div className="studio">
      <div className="studio-top">
        <span className="media-name">{name}</span>
        <button className="chip" onClick={onClose}>Load another song</button>
      </div>
      <div className="transport">
        <button className="wave-btn" onClick={() => (playing ? pause() : play())}>
          {playing ? (
            <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
              <rect x="6" y="5" width="4" height="14" rx="1" /><rect x="14" y="5" width="4" height="14" rx="1" />
            </svg>
          ) : (
            <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
              <path d="M8 5.5v13a1 1 0 0 0 1.5.87l11-6.5a1 1 0 0 0 0-1.74l-11-6.5A1 1 0 0 0 8 5.5z" />
            </svg>
          )}
        </button>
        <div className="sbar" onClick={e => {
          const r = e.currentTarget.getBoundingClientRect()
          seek(((e.clientX - r.left) / r.width) * duration)
        }}>
          <div className="sbar-fill" style={{ width: `${duration ? (pos / duration) * 100 : 0}%` }} />
        </div>
        <span className="wave-time">{fmtTime(pos)} / {fmtTime(duration)}</span>
      </div>

      {ORDER.filter(k => stems[k]).map(k => (
        <div key={k} className={'track-row' + (effGain(k) === 0 ? ' off' : '')}>
          <span className="track-name">{STEM_NAMES[k]}</span>
          <button className={'chip tiny' + (muted[k] ? ' active' : '')} onClick={() => toggleMute(k)}>M</button>
          <button className={'chip tiny' + (solo === k ? ' active' : '')} onClick={() => toggleSolo(k)}>S</button>
          <input type="range" min="0" max="1.5" step="0.01" value={gains[k]}
            onChange={e => setGain(k, parseFloat(e.target.value))} />
          <span className="track-pct">{Math.round(gains[k] * 100)}%</span>
        </div>
      ))}

      <div style={{ marginTop: 20, display: 'flex', justifyContent: 'center' }}>
        <LiquidMetalButton label="Export mix" width={160} onClick={exportMix} disabled={expBusy} />
      </div>
      <Status {...(expStatus || {})} />
      {expResult && (
        <div className="results">
          <MediaCard title={expResult.name} url={expResult.file} accent="#e8e8e8" />
        </div>
      )}
    </div>
  )
}
