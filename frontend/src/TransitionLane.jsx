import { useEffect, useRef, useState } from 'react'
import { fmtTime } from './Waveform'

/* One deck lane for the transition editor: waveform with detected sections
   painted by energy, plus a draggable bar-snapped marker. */
export function TransitionLane({ src, info, marker, onMarker, label, note }) {
  const canvasRef = useRef(null)
  const audioRef = useRef(null)
  const dragRef = useRef(false)
  const [peaks, setPeaks] = useState(null)
  const [playing, setPlaying] = useState(false)

  const duration = info.duration

  function snap(t) {
    if (info.downbeats && info.downbeats.length) {
      let best = info.downbeats[0], bd = Infinity
      for (const d of info.downbeats) {
        const diff = Math.abs(d - t)
        if (diff < bd) { bd = diff; best = d }
      }
      return best
    }
    const k = Math.round((t - info.bar_phase) / info.bar_len)
    return Math.min(Math.max(info.bar_phase + k * info.bar_len, 0), duration - 1)
  }

  useEffect(() => {
    let cancelled = false
    setPeaks(null)
    ;(async () => {
      try {
        const buf = await (await fetch(encodeURI(src))).arrayBuffer()
        const actx = new (window.AudioContext || window.webkitAudioContext)()
        const audio = await actx.decodeAudioData(buf)
        actx.close()
        if (cancelled) return
        const ch = audio.getChannelData(0)
        const N = 700
        const step = Math.max(1, Math.floor(ch.length / N))
        const stride = Math.max(1, Math.floor(step / 40))
        const p = new Float32Array(N)
        for (let i = 0; i < N; i++) {
          let m = 0
          const lim = Math.min((i + 1) * step, ch.length)
          for (let j = i * step; j < lim; j += stride) {
            const v = Math.abs(ch[j])
            if (v > m) m = v
          }
          p[i] = m
        }
        setPeaks(p)
      } catch {}
    })()
    return () => { cancelled = true }
  }, [src])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || !peaks) return
    const draw = () => {
      const dpr = window.devicePixelRatio || 1
      const w = canvas.offsetWidth, h = canvas.offsetHeight
      if (!w) return
      canvas.width = w * dpr
      canvas.height = h * dpr
      const ctx = canvas.getContext('2d')
      ctx.scale(dpr, dpr)
      ctx.clearRect(0, 0, w, h)
      // section stripes + boundaries
      for (let i = 0; i < (info.sections || []).length; i++) {
        const s = info.sections[i]
        const x0 = (s.start / duration) * w
        const x1 = (s.end / duration) * w
        if (i % 2 === 1) {
          ctx.fillStyle = 'rgba(255,255,255,0.035)'
          ctx.fillRect(x0, 0, x1 - x0, h)
        }
        ctx.fillStyle = 'rgba(255,255,255,0.10)'
        ctx.fillRect(x0, 0, 1, h)
      }
      // waveform, brightness follows section energy
      const mid = h / 2
      const bw = w / peaks.length
      const energyAt = t => {
        for (const s of info.sections || []) {
          if (t >= s.start && t < s.end) return s.energy
        }
        return 0.6
      }
      for (let i = 0; i < peaks.length; i++) {
        const x = i * bw
        const t = (i / peaks.length) * duration
        const alpha = 0.14 + 0.55 * energyAt(t)
        const half = Math.max(0.8, peaks[i] * mid * 0.94)
        ctx.fillStyle = `rgba(255,255,255,${alpha.toFixed(3)})`
        ctx.fillRect(x, mid - half, Math.max(1, bw * 0.72), half * 2)
      }
      // marker
      const mx = (marker / duration) * w
      ctx.fillStyle = '#ffffff'
      ctx.fillRect(mx - 1, 0, 2, h)
      ctx.beginPath()
      ctx.moveTo(mx - 6, 0)
      ctx.lineTo(mx + 6, 0)
      ctx.lineTo(mx, 8)
      ctx.closePath()
      ctx.fill()
    }
    draw()
    window.addEventListener('resize', draw)
    return () => window.removeEventListener('resize', draw)
  }, [peaks, marker, info, duration])

  function timeAt(e) {
    const rect = canvasRef.current.getBoundingClientRect()
    const x = Math.min(Math.max(e.clientX - rect.left, 0), rect.width)
    return (x / rect.width) * duration
  }

  function toggleAudition() {
    const a = audioRef.current
    if (!a) return
    if (playing) { a.pause() } else {
      a.currentTime = Math.max(0, marker - 4)
      a.play()
    }
  }

  return (
    <div className="lane">
      <div className="lane-head">
        <span className="lane-label">{label} <span className="lane-note">{note}</span></span>
        <span className="lane-actions">
          <button className="chip tiny" onClick={toggleAudition}>
            {playing ? '◼ stop' : '▶ from marker'}
          </button>
          <span className="lane-time">{fmtTime(marker)}</span>
        </span>
      </div>
      <div className="lane-screen">
        {!peaks && <div className="wave-loading" style={{ height: 88 }}>building waveform…</div>}
        <canvas
          ref={canvasRef}
          style={{ width: '100%', height: 88, cursor: 'ew-resize', touchAction: 'none',
                   display: peaks ? 'block' : 'none' }}
          onPointerDown={e => {
            e.currentTarget.setPointerCapture(e.pointerId)
            dragRef.current = true
            onMarker(snap(timeAt(e)))
          }}
          onPointerMove={e => { if (dragRef.current) onMarker(snap(timeAt(e))) }}
          onPointerUp={() => { dragRef.current = false }}
        />
      </div>
      <audio ref={audioRef} src={encodeURI(src)} hidden
        onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)} />
    </div>
  )
}
