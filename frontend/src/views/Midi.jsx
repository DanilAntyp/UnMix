import { useEffect, useRef, useState } from 'react'
import { postForm, postJSON } from '../api'
import { DropZone, Status, PanelHead, IconPiano } from '../ui'
import { LiquidMetalButton } from '../LiquidMetalButton'

function PianoRoll({ notes }) {
  const ref = useRef(null)
  useEffect(() => {
    const canvas = ref.current
    if (!canvas || !notes.length) return
    const draw = () => {
      const dpr = window.devicePixelRatio || 1
      const w = canvas.offsetWidth, h = canvas.offsetHeight
      canvas.width = w * dpr
      canvas.height = h * dpr
      const ctx = canvas.getContext('2d')
      ctx.scale(dpr, dpr)
      ctx.fillStyle = '#0c0c0c'
      ctx.fillRect(0, 0, w, h)
      const tEnd = Math.max(...notes.map(n => n.end))
      const pMin = Math.min(...notes.map(n => n.pitch)) - 2
      const pMax = Math.max(...notes.map(n => n.pitch)) + 2
      const px = t => (t / tEnd) * w
      const py = p => h - ((p - pMin) / (pMax - pMin)) * h
      const rowH = h / (pMax - pMin)
      // octave guide lines (C notes)
      ctx.strokeStyle = 'rgba(255,255,255,0.07)'
      for (let p = Math.ceil(pMin / 12) * 12; p <= pMax; p += 12) {
        ctx.beginPath()
        ctx.moveTo(0, py(p))
        ctx.lineTo(w, py(p))
        ctx.stroke()
      }
      for (const n of notes) {
        ctx.fillStyle = '#e8e8e8'
        ctx.fillRect(px(n.start), py(n.pitch) - rowH, Math.max(2, px(n.end) - px(n.start) - 1), Math.max(2, rowH - 1))
      }
    }
    draw()
    window.addEventListener('resize', draw)
    return () => window.removeEventListener('resize', draw)
  }, [notes])
  return <canvas ref={ref} style={{ width: '100%', height: 260, borderRadius: 14, display: 'block' }} />
}

export function MidiView() {
  const [picked, setPicked] = useState(null)
  const [status, setStatus] = useState(null)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)

  function pick(f) {
    setPicked(f); setResult(null); setStatus(null)
  }

  async function run() {
    if (!picked || busy) return
    setBusy(true); setResult(null)
    setStatus({ busy: true, text: 'Uploading…' })
    try {
      const fd = new FormData()
      fd.append('audio', picked)
      const up = await postForm('/upload', fd)
      setStatus({ busy: true, text: 'Listening for notes…' })
      const data = await postJSON('/midi', { server_file: up.file })
      if (!data.notes?.length) throw new Error('no clear notes found — try a cleaner stem')
      setStatus({ text: `Done! ${data.count} notes transcribed.` })
      setResult(data)
    } catch (e) {
      setStatus({ text: 'Error: ' + e.message, error: true })
    } finally { setBusy(false) }
  }

  return (
    <div className="glass metal-scope">
      <PanelHead
        tile="tile-mint" icon={<IconPiano />}
        title="Catch the melody" sub="Turn a vocal, bassline, or instrument into editable MIDI notes."
      />
      <DropZone onFile={pick} icon={<IconPiano width={30} height={30} color="#9a9aa8" />}
        hint="works best on a single instrument — extract a stem first (bass, vocals)"
        accept="audio/*,.mp3,.wav,.flac,.m4a,.ogg,.aac" />
      {picked && <div className="picked">♪ {picked.name}</div>}

      {picked && (
        <div style={{ marginTop: 18, display: 'flex', justifyContent: 'center' }}>
          <LiquidMetalButton label="Transcribe to MIDI" width={190} onClick={run} disabled={busy} />
        </div>
      )}
      <Status {...(status || {})} />

      {result && (
        <div className="results">
          <div className="media-card glass-soft">
            <div className="media-row">
              <span className="media-name">Piano roll · {result.count} notes</span>
              <a className="chip grad" href={result.file} download>Download .mid</a>
            </div>
            <PianoRoll notes={result.notes} />
          </div>
        </div>
      )}
    </div>
  )
}
