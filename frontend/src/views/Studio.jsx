import { useEffect, useRef, useState } from 'react'
import { postForm, postJSON, pollProgress, STEM_NAMES } from '../api'
import { DropZone, Status, MediaCard, PanelHead, IconSliders } from '../ui'
import { LiquidMetalButton } from '../LiquidMetalButton'
import { fmtTime } from '../Waveform'

const ORDER = ['vocals', 'drums', 'bass', 'other']
const COLS = 700

export function StudioView({ handoff }) {
  const [picked, setPicked] = useState(null)
  const [songName, setSongName] = useState('')
  const [stems, setStems] = useState(null)
  const [status, setStatus] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (handoff?.file) {
      setPicked(null)
      openServer(handoff.file,
        decodeURIComponent(handoff.file.split('/').pop()).replace(/\.[^.]+$/, ''))
    }
  }, [handoff])

  function pick(f) {
    setPicked(f); setStems(null); setStatus(null)
  }

  async function openServer(serverFile, name) {
    setBusy(true); setStems(null)
    const stop = pollProgress('/progress/sep', pct =>
      setStatus({ busy: true, text: 'Separating into stems…', pct }))
    try {
      setStatus({ busy: true, text: 'Separating into stems…', pct: 0 })
      const data = await postJSON('/separate', { server_file: decodeURI(serverFile), remove: 'all', model: 'htdemucs' })
      stop()
      setStatus(null)
      setSongName(name)
      setStems(data.all)
    } catch (e) {
      stop()
      setStatus({ text: 'Error: ' + e.message, error: true })
    } finally { setBusy(false) }
  }

  async function split() {
    if (!picked || busy) return
    setBusy(true)
    setStatus({ busy: true, text: 'Uploading…' })
    try {
      const fd = new FormData()
      fd.append('audio', picked)
      const up = await postForm('/upload', fd)
      await openServer(up.file, up.name.replace(/\.[^.]+$/, ''))
    } catch (e) {
      setStatus({ text: 'Error: ' + e.message, error: true })
      setBusy(false)
    }
  }

  return (
    <div className="glass metal-scope">
      <PanelHead
        tile="tile-violet" icon={<IconSliders />}
        title="The fader room" sub="A fresh balance. A different perspective. Remix the stems your way."
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
      {stems && <StudioPlayer stems={stems} name={songName}
        onClose={() => { setStems(null); setPicked(null); setStatus(null) }} />}
    </div>
  )
}

/* Main mix waveform: per-stem column peaks are precomputed once; the drawn
   height of each column follows the live fader gains, so muting the bass
   visibly thins the waveform. Click plays from there, drag selects a region. */
function StudioWave({ amps, effGains, pos, duration, sel, onSeekPlay, onSelect }) {
  const ref = useRef(null)
  const dragRef = useRef(null)

  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const draw = () => {
      const dpr = window.devicePixelRatio || 1
      const w = canvas.offsetWidth, h = canvas.offsetHeight
      if (!w) return
      canvas.width = w * dpr
      canvas.height = h * dpr
      const ctx = canvas.getContext('2d')
      ctx.scale(dpr, dpr)
      ctx.clearRect(0, 0, w, h)
      const mid = h / 2
      const bw = w / COLS
      const playedX = duration ? (pos / duration) * w : 0
      for (let i = 0; i < COLS; i++) {
        let a = 0
        for (const k in amps) a += (effGains[k] || 0) * amps[k][i]
        a = Math.min(1, a)
        const half = Math.max(0.8, a * mid * 0.94)
        const x = i * bw
        ctx.fillStyle = x <= playedX ? '#e8e8e8' : 'rgba(255,255,255,0.25)'
        ctx.fillRect(x, mid - half, Math.max(1, bw * 0.72), half * 2)
      }
      if (sel && duration) {
        const x1 = (sel[0] / duration) * w
        const x2 = (sel[1] / duration) * w
        ctx.fillStyle = 'rgba(255,255,255,0.13)'
        ctx.fillRect(x1, 0, x2 - x1, h)
        ctx.fillStyle = 'rgba(255,255,255,0.75)'
        ctx.fillRect(x1, 0, 1.5, h)
        ctx.fillRect(x2 - 1.5, 0, 1.5, h)
      }
    }
    draw()
    window.addEventListener('resize', draw)
    return () => window.removeEventListener('resize', draw)
  }, [amps, effGains, pos, duration, sel])

  function timeAt(e) {
    const rect = ref.current.getBoundingClientRect()
    const x = Math.min(Math.max(e.clientX - rect.left, 0), rect.width)
    return (x / rect.width) * duration
  }
  function down(e) {
    if (!duration) return
    e.currentTarget.setPointerCapture(e.pointerId)
    dragRef.current = { t0: timeAt(e), moved: false, cur: null }
  }
  function move(e) {
    const d = dragRef.current
    if (!d) return
    const t = timeAt(e)
    if (Math.abs(t - d.t0) > duration / 200) d.moved = true
    if (d.moved) {
      d.cur = [Math.min(d.t0, t), Math.max(d.t0, t)]
      onSelect(d.cur)
    }
  }
  function up(e) {
    const d = dragRef.current
    dragRef.current = null
    if (!d) return
    if (!d.moved) {
      onSelect(null)
      onSeekPlay(timeAt(e))
    }
  }

  return (
    <canvas ref={ref}
      style={{ width: '100%', height: 110, cursor: 'crosshair', touchAction: 'none', display: 'block' }}
      onPointerDown={down} onPointerMove={move} onPointerUp={up} />
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
  const [amps, setAmps] = useState(null)     // k -> Float32Array(COLS) column peaks
  const [playing, setPlaying] = useState(false)
  const [pos, setPos] = useState(0)
  const [duration, setDuration] = useState(0)
  const [sel, setSel] = useState(null)
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
      const a = {}
      for (const [k, b] of entries) {
        const ch = b.getChannelData(0)
        const col = new Float32Array(COLS)
        const step = Math.max(1, Math.floor(ch.length / COLS))
        const stride = Math.max(1, Math.floor(step / 40))
        for (let i = 0; i < COLS; i++) {
          let m = 0
          const lim = Math.min((i + 1) * step, ch.length)
          for (let j = i * step; j < lim; j += stride) {
            const v = Math.abs(ch[j])
            if (v > m) m = v
          }
          col[i] = m
        }
        a[k] = col
      }
      setAmps(a)
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
  const effGains = Object.fromEntries(ORDER.map(k => [k, effGain(k, { gains, muted, solo })]))

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

  function seekPlay(t) {
    posRef.current = t
    setPos(t)
    play(t)  // click on the waveform starts playback from there
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
      const body = { name, tracks }
      if (sel) { body.start = sel[0]; body.end = sel[1] }
      const data = await postJSON('/studio/export', body)
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
        <div style={{ flex: 1, minWidth: 0 }}>
          <StudioWave amps={amps} effGains={effGains} pos={pos} duration={duration}
            sel={sel} onSeekPlay={seekPlay} onSelect={setSel} />
          <div className="wave-hint">
            {sel
              ? `selected ${fmtTime(sel[0])} – ${fmtTime(sel[1])} — export will keep only this part (click to clear)`
              : 'click to play from there · drag to select the part to export'}
          </div>
        </div>
        <span className="wave-time">{fmtTime(pos)} / {fmtTime(duration)}</span>
      </div>

      {ORDER.filter(k => stems[k]).map(k => (
        <div key={k} className={'track-row' + (effGains[k] === 0 ? ' off' : '')}>
          <span className="track-name">{STEM_NAMES[k]}</span>
          <button className={'chip tiny' + (muted[k] ? ' active' : '')} onClick={() => toggleMute(k)}>M</button>
          <button className={'chip tiny' + (solo === k ? ' active' : '')} onClick={() => toggleSolo(k)}>S</button>
          <input type="range" min="0" max="1.5" step="0.01" value={gains[k]}
            onChange={e => setGain(k, parseFloat(e.target.value))} />
          <span className="track-pct">{Math.round(gains[k] * 100)}%</span>
        </div>
      ))}

      <div style={{ marginTop: 20, display: 'flex', justifyContent: 'center' }}>
        <LiquidMetalButton label={sel ? 'Export selection' : 'Export mix'} width={180}
          onClick={exportMix} disabled={expBusy} />
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
