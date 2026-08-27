import { useEffect, useRef, useState } from 'react'

export function fmtTime(s) {
  if (s == null || isNaN(s)) return '0:00'
  const m = Math.floor(s / 60)
  const sec = Math.floor(s % 60)
  return `${m}:${String(sec).padStart(2, '0')}`
}

// precise form for trim fields: m:ss.t
export function fmtTimePrecise(s) {
  const m = Math.floor(s / 60)
  const sec = (s % 60).toFixed(1).padStart(4, '0')
  return `${m}:${sec}`
}

const PlayIcon = ({ playing }) => playing ? (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
    <rect x="6" y="5" width="4" height="14" rx="1" /><rect x="14" y="5" width="4" height="14" rx="1" />
  </svg>
) : (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
    <path d="M8 5.5v13a1 1 0 0 0 1.5.87l11-6.5a1 1 0 0 0 0-1.74l-11-6.5A1 1 0 0 0 8 5.5z" />
  </svg>
)

/**
 * Canvas waveform player.
 *  - click to seek
 *  - drag to select a region (when selectable); onSelect([a, b] | null)
 */
export function Waveform({ src, selectable, onSelect, height = 96 }) {
  const canvasRef = useRef(null)
  const audioRef = useRef(null)
  const dragRef = useRef(null)
  const [peaks, setPeaks] = useState(null) // array | 'error' | null (loading)
  const [duration, setDuration] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [pos, setPos] = useState(0)
  const [sel, setSel] = useState(null)

  /* decode audio -> peaks */
  useEffect(() => {
    let cancelled = false
    setPeaks(null); setSel(null); setPos(0); setPlaying(false)
    onSelect && onSelect(null)
    ;(async () => {
      try {
        const buf = await (await fetch(src)).arrayBuffer()
        const actx = new (window.AudioContext || window.webkitAudioContext)()
        const audio = await actx.decodeAudioData(buf)
        actx.close()
        if (cancelled) return
        const ch = audio.getChannelData(0)
        const N = 700
        const step = Math.max(1, Math.floor(ch.length / N))
        const stride = Math.max(1, Math.floor(step / 60))
        const p = new Array(N)
        for (let i = 0; i < N; i++) {
          let mn = 1, mx = -1
          const lim = Math.min((i + 1) * step, ch.length)
          for (let j = i * step; j < lim; j += stride) {
            const v = ch[j]
            if (v < mn) mn = v
            if (v > mx) mx = v
          }
          p[i] = [mn === 1 ? 0 : mn, mx === -1 ? 0 : mx]
        }
        setPeaks(p)
        setDuration(audio.duration)
      } catch {
        if (!cancelled) setPeaks('error')
      }
    })()
    return () => { cancelled = true }
  }, [src])

  /* draw */
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || !Array.isArray(peaks)) return
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
      const n = peaks.length
      const bw = w / n
      const playedX = duration ? (pos / duration) * w : 0
      for (let i = 0; i < n; i++) {
        const [mn, mx] = peaks[i]
        const x = i * bw
        let y1 = mid + mn * mid * 0.94
        let y2 = mid + mx * mid * 0.94
        if (y2 - y1 < 1.6) { y1 = mid - 0.8; y2 = mid + 0.8 }
        ctx.fillStyle = x <= playedX ? '#aaff00' : 'rgba(255,255,255,0.25)'
        ctx.fillRect(x, Math.min(y1, y2), Math.max(1, bw * 0.72), Math.abs(y2 - y1))
      }
      if (sel && duration) {
        const x1 = (sel[0] / duration) * w
        const x2 = (sel[1] / duration) * w
        ctx.fillStyle = 'rgba(170,255,0,0.16)'
        ctx.fillRect(x1, 0, x2 - x1, h)
        ctx.fillStyle = 'rgba(170,255,0,0.85)'
        ctx.fillRect(x1, 0, 1.5, h)
        ctx.fillRect(x2 - 1.5, 0, 1.5, h)
      }
    }
    draw()
    window.addEventListener('resize', draw)
    return () => window.removeEventListener('resize', draw)
  }, [peaks, pos, sel, duration])

  function timeAt(e) {
    const rect = canvasRef.current.getBoundingClientRect()
    const x = Math.min(Math.max(e.clientX - rect.left, 0), rect.width)
    return (x / rect.width) * duration
  }

  function onPointerDown(e) {
    if (!Array.isArray(peaks) || !duration) return
    e.currentTarget.setPointerCapture(e.pointerId)
    dragRef.current = { t0: timeAt(e), moved: false }
  }
  function onPointerMove(e) {
    const d = dragRef.current
    if (!d) return
    const t = timeAt(e)
    if (Math.abs(t - d.t0) > duration / 200) d.moved = true
    if (d.moved && selectable) {
      const range = [Math.min(d.t0, t), Math.max(d.t0, t)]
      setSel(range)
    }
  }
  function onPointerUp(e) {
    const d = dragRef.current
    dragRef.current = null
    if (!d) return
    if (d.moved && selectable && sel) {
      onSelect && onSelect(sel)
    } else {
      const t = timeAt(e)
      if (audioRef.current) audioRef.current.currentTime = t
      setPos(t)
      setSel(null)
      onSelect && onSelect(null)
    }
  }

  function toggle() {
    const a = audioRef.current
    if (!a) return
    if (playing) a.pause()
    else a.play()
  }

  return (
    <div className="wave">
      <button className="wave-btn" onClick={toggle} disabled={!Array.isArray(peaks)}>
        <PlayIcon playing={playing} />
      </button>
      <div className="wave-body">
        {peaks === null && <div className="wave-loading">building waveform…</div>}
        {peaks === 'error' && <div className="wave-loading">could not decode audio</div>}
        <canvas
          ref={canvasRef}
          style={{ width: '100%', height, display: Array.isArray(peaks) ? 'block' : 'none' }}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
        />
        {selectable && Array.isArray(peaks) && (
          <div className="wave-hint">
            {sel
              ? `selected ${fmtTime(sel[0])} – ${fmtTime(sel[1])} (click waveform to clear)`
              : 'click to seek · drag to select a region to trim'}
          </div>
        )}
      </div>
      <span className="wave-time">{fmtTime(pos)} / {fmtTime(duration)}</span>
      <audio
        ref={audioRef} src={src} hidden
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        onTimeUpdate={e => setPos(e.target.currentTime)}
      />
    </div>
  )
}
