import { useEffect, useRef, useState } from 'react'
import { Waveform } from './Waveform'

/* ---------- BPM + key badges (server-side files only) ---------- */
export function TrackFacts({ url }) {
  const [facts, setFacts] = useState(null)
  useEffect(() => {
    let cancelled = false
    setFacts(null)
    fetch('/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ server_file: decodeURI(url) }),
    })
      .then(r => r.json())
      .then(d => { if (!cancelled) setFacts(d) })
      .catch(() => { if (!cancelled) setFacts({ error: true }) })
    return () => { cancelled = true }
  }, [url])
  if (facts?.error) return null
  if (!facts) return <span className="facts dim">analyzing…</span>
  return (
    <span className="facts" title={facts.key}>
      {Math.round(facts.bpm)} BPM{facts.camelot ? ` · ${facts.camelot}` : ''}
      {facts.key ? <span className="facts-key"> {facts.key}</span> : null}
    </span>
  )
}

/* ---------- inline stroke icons ---------- */
const I = props => ({
  viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
  strokeWidth: 1.9, strokeLinecap: 'round', strokeLinejoin: 'round', ...props,
})

export const IconDownload = p => (
  <svg {...I(p)}><path d="M12 3v12m0 0l-4.5-4.5M12 15l4.5-4.5M4 19h16" /></svg>
)
export const IconWave = p => (
  <svg {...I(p)}><path d="M3 12h2m2-4v8m3-12v16m3-11v6m3-9v12m3-8v4m3-2h2" /></svg>
)
export const IconMic = p => (
  <svg {...I(p)}>
    <rect x="9" y="3" width="6" height="11" rx="3" />
    <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
  </svg>
)
export const IconScissors = p => (
  <svg {...I(p)}>
    <circle cx="6" cy="6" r="2.6" /><circle cx="6" cy="18" r="2.6" />
    <path d="M8.2 7.6L20 19M8.2 16.4L20 5" />
  </svg>
)
export const IconSpark = p => (
  <svg {...I(p)}><path d="M12 3l1.9 5.6L19.5 10l-5.6 1.9L12 17.5l-1.9-5.6L4.5 10l5.6-1.4L12 3zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8L19 16z" /></svg>
)
export const IconNote = p => (
  <svg {...I(p)}>
    <circle cx="7" cy="18" r="2.6" /><circle cx="17" cy="16" r="2.6" />
    <path d="M9.6 18V6.5L19.6 4v12" />
  </svg>
)
export const IconSliders = p => (
  <svg {...I(p)}>
    <path d="M5 4v6m0 4v6m7-16v2m0 4v10m7-16v10m0 4v2" />
    <circle cx="5" cy="12" r="2" /><circle cx="12" cy="8" r="2" /><circle cx="19" cy="16" r="2" />
  </svg>
)
export const IconBlend = p => (
  <svg {...I(p)}>
    <circle cx="9" cy="12" r="5.5" /><circle cx="15" cy="12" r="5.5" />
  </svg>
)
export const IconPiano = p => (
  <svg {...I(p)}>
    <rect x="3" y="5" width="18" height="14" rx="2" />
    <path d="M8 12v7m4-7v7m4-7v7M8 5v7h8V5" />
  </svg>
)

/* ---------- drop zone ---------- */
export function DropZone({ onFile, hint, icon, accept }) {
  const inputRef = useRef()
  const [hover, setHover] = useState(false)
  const prevent = e => e.preventDefault()
  return (
    <div
      className={'drop' + (hover ? ' hover' : '')}
      onClick={() => inputRef.current.click()}
      onDragEnter={e => { prevent(e); setHover(true) }}
      onDragOver={e => { prevent(e); setHover(true) }}
      onDragLeave={e => { prevent(e); setHover(false) }}
      onDrop={e => {
        prevent(e); setHover(false)
        const f = e.dataTransfer.files[0]
        if (f) onFile(f)
      }}
    >
      <div className="drop-icon">{icon}</div>
      <p><strong>Drop a file here</strong> or click to choose</p>
      {hint && <span className="drop-hint">{hint}</span>}
      <input
        ref={inputRef} type="file" hidden
        accept={accept || 'audio/*,video/*,.mp3,.wav,.flac,.m4a,.ogg,.aac'}
        onChange={e => { if (e.target.files[0]) onFile(e.target.files[0]); e.target.value = '' }}
      />
    </div>
  )
}

/* ---------- status line with optional progress bar ---------- */
export function Status({ busy, text, pct, error }) {
  if (!text) return <div className="status" />
  return (
    <div className={'status' + (error ? ' error' : '')}>
      {busy && <span className="spinner" />}
      <span>{text}{pct != null ? ` — ${Math.round(pct)}%` : ''}</span>
      {busy && pct != null && (
        <div className="bar"><div className="bar-fill" style={{ width: `${pct}%` }} /></div>
      )}
    </div>
  )
}

/* ---------- result card with player ---------- */
export function MediaCard({ title, url, video, actions, accent }) {
  const safe = encodeURI(url)
  const name = decodeURIComponent(url.split('/').pop())
  return (
    <div className="media-card glass-soft">
      <div className="media-row">
        <span className="media-name" title={name}>{title}</span>
        {!video && <TrackFacts url={url} />}
        <span className="media-actions">
          {actions}
          <a className="chip" href={safe} download={name}>Download</a>
        </span>
      </div>
      {video ? <video controls src={safe} /> : <Waveform src={safe} height={72} accent={accent} />}
    </div>
  )
}

/* ---------- panel header with gradient tile ---------- */
export function PanelHead({ tile, icon, title, sub }) {
  return (
    <div className="panel-head">
      <div className={'tile ' + tile}>{icon}</div>
      <div>
        <h2>{title}</h2>
        <p>{sub}</p>
      </div>
    </div>
  )
}
