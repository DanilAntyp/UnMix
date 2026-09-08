import { useEffect, useRef, useState } from 'react'
import { postJSON } from '../api'
import { Status, MediaCard, PanelHead, TrackFacts, IconNote } from '../ui'
import { LiquidMetalButton } from '../LiquidMetalButton'
import { Waveform, fmtTime } from '../Waveform'
import { TransitionLane } from '../TransitionLane'

const JOIN_STYLES = [
  { id: 'automix', name: 'AutoMix blend' },
  { id: 'tapestop', name: 'Tape stop' },
  { id: 'backspin', name: 'Backspin' },
  { id: 'looproll', name: 'Loop roll' },
  { id: 'riser', name: 'Riser' },
  { id: 'cut', name: 'Cut' },
]

function cueText(tracklist, filename) {
  const lines = [`FILE "${filename}" MP3`]
  tracklist.forEach((t, i) => {
    const m = Math.floor(t.at / 60)
    const s = Math.floor(t.at % 60)
    const f = Math.floor((t.at % 1) * 75)
    lines.push(`  TRACK ${String(i + 1).padStart(2, '0')} AUDIO`)
    lines.push(`    TITLE "${t.title.replace(/"/g, "'")}"`)
    lines.push(`    INDEX 01 ${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}:${String(f).padStart(2, '0')}`)
  })
  return lines.join('\n')
}

function tracklistText(tracklist) {
  return tracklist.map(t => `${fmtTime(t.at)} ${t.title}`).join('\n')
}

export function SetView() {
  const [files, setFiles] = useState([])
  const [picked, setPicked] = useState({})
  const [beats, setBeats] = useState('auto')
  const [plan, setPlan] = useState(null)       // {tracks, joins}
  const [status, setStatus] = useState(null)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const [joinPreview, setJoinPreview] = useState({})  // idx -> {busy, file}
  const [joinEdit, setJoinEdit] = useState({})        // idx -> {open, busy, inspect, cut, bStart}
  const [copied, setCopied] = useState(false)
  const pollRef = useRef(null)
  const previewPollRef = useRef({})   // join idx -> timer, so previews can be stopped too

  useEffect(() => {
    fetch('/files').then(r => r.json()).then(d => setFiles(d.files || [])).catch(() => {})
    return () => {
      clearTimeout(pollRef.current)
      Object.values(previewPollRef.current).forEach(clearTimeout)
    }
  }, [])

  const chosen = files.filter(f => picked[f.file])

  function pollJob(id, onDone) {
    const tick = async () => {
      try {
        const j = await (await fetch('/dj/status/' + id)).json()
        if (j.error) throw new Error(j.error)
        if (!j.done) {
          setStatus({ busy: true, text: j.stage, pct: j.pct })
          pollRef.current = setTimeout(tick, 1500)
          return
        }
        onDone(j)
      } catch (e) {
        setStatus({ text: 'Error: ' + e.message, error: true })
        setBusy(false)
      }
    }
    tick()
  }

  async function makePlan() {
    if (chosen.length < 2 || busy) return
    setBusy(true); setResult(null); setPlan(null)
    setStatus({ busy: true, text: 'Planning…' })
    try {
      const d = await postJSON('/dj/set/plan', { files: chosen.map(f => f.file), beats })
      pollJob(d.job, j => {
        setPlan(j.plan)
        setStatus({ text: j.stage })
        setBusy(false)
      })
    } catch (e) {
      setStatus({ text: 'Error: ' + e.message, error: true })
      setBusy(false)
    }
  }

  function move(i, dir) {
    setPlan(p => {
      const tracks = [...p.tracks]
      const j = i + dir
      if (j < 0 || j >= tracks.length) return p
      ;[tracks[i], tracks[j]] = [tracks[j], tracks[i]]
      return { ...p, tracks }
    })
    setJoinPreview({})
    setJoinEdit({})
  }

  async function toggleJoinEdit(i) {
    const cur = joinEdit[i]
    if (cur?.open) { setJoinEdit(p => ({ ...p, [i]: { ...p[i], open: false } })); return }
    if (cur?.inspect) { setJoinEdit(p => ({ ...p, [i]: { ...p[i], open: true } })); return }
    setJoinEdit(p => ({ ...p, [i]: { open: true, busy: true } }))
    try {
      const d = await postJSON('/dj/inspect', {
        a_file: decodeURI(plan.tracks[i].file),
        b_file: decodeURI(plan.tracks[i + 1].file),
        beats,
      })
      setJoinEdit(p => ({
        ...p,
        [i]: { ...p[i], busy: false, inspect: d, cut: d.a.cut, bStart: d.b.b_start },
      }))
    } catch (e) {
      setJoinEdit(p => ({ ...p, [i]: { open: true, busy: false, error: e.message } }))
    }
  }

  // only send a marker when the user actually moved it off the proposal
  function joinOverrides(i) {
    const je = joinEdit[i]
    if (!je?.inspect) return {}
    const out = {}
    if (je.cut !== je.inspect.a.cut) out.cut = je.cut
    if (je.bStart !== je.inspect.b.b_start) out.b_start = je.bStart
    return out
  }

  function setJoinStyle(i, style) {
    setPlan(p => {
      const joins = [...p.joins]
      joins[i] = { ...joins[i], style }
      return { ...p, joins }
    })
  }

  async function previewJoin(i) {
    const a = plan.tracks[i].file
    const b = plan.tracks[i + 1].file
    const style = JOIN_STYLES.some(s => s.id === plan.joins[i]?.style)
      ? plan.joins[i].style : 'automix'
    setJoinPreview(p => ({ ...p, [i]: { busy: true } }))
    try {
      const d = await postJSON('/dj/start', {
        a_file: decodeURI(a), b_file: decodeURI(b),
        preview: true, styles: [style], beats,
        ...joinOverrides(i),
      })
      const tick = async () => {
        try {
          const j = await (await fetch('/dj/status/' + d.job)).json()
          if (j.error) throw new Error(j.error)
          if (!j.done) {
            // keep the handle so leaving the page stops the poll
            previewPollRef.current[i] = setTimeout(tick, 1500)
            return
          }
          const clip = j.previews?.find(p2 => !p2.error)
          setJoinPreview(p => ({ ...p, [i]: clip ? { file: clip.file } : { error: true } }))
        } catch {
          setJoinPreview(p => ({ ...p, [i]: { error: true } }))
        }
      }
      tick()
    } catch {
      setJoinPreview(p => ({ ...p, [i]: { error: true } }))
    }
  }

  async function render() {
    if (!plan || busy) return
    setBusy(true); setResult(null)
    setStatus({ busy: true, text: 'Starting…' })
    try {
      const d = await postJSON('/dj/set/start', {
        files: plan.tracks.map(t => decodeURI(t.file)),
        join_styles: plan.joins.map(j => j.style),
        cuts: plan.joins.map((_, i) => joinOverrides(i).cut ?? null),
        b_starts: plan.joins.map((_, i) => joinOverrides(i).b_start ?? null),
        ordered: true, beats,
      })
      pollJob(d.job, j => {
        setStatus({ text: 'Done!' })
        setResult(j)
        setBusy(false)
      })
    } catch (e) {
      setStatus({ text: 'Error: ' + e.message, error: true })
      setBusy(false)
    }
  }

  // green zones on the rendered set: one per join, around each tracklist mark
  function setMarks() {
    if (!result?.tracklist) return undefined
    return result.tracklist.slice(1).map((t, i) => {
      const styleUsed = plan?.joins[i]?.style || 'automix'
      const beat = 60 / (plan?.tracks?.[0]?.bpm || 125)  // chain runs near track 1's tempo
      const nb = result?.join_beats?.[i] ?? (beats === 'auto' ? 32 : beats)
      return styleUsed === 'automix'
        ? { start: t.at - (nb * beat) / 2, end: t.at + (nb * beat) / 2 }
        : { start: t.at - 4 * beat, end: t.at + 2 * beat }
    })
  }

  function copyTracklist() {
    navigator.clipboard?.writeText(tracklistText(result.tracklist)).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }

  function downloadCue() {
    const name = decodeURIComponent(result.file.split('/').pop())
    const blob = new Blob([cueText(result.tracklist, name)], { type: 'text/plain' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = name.replace(/\.mp3$/, '.cue')
    a.click()
    URL.revokeObjectURL(a.href)
  }

  return (
    <div className="glass metal-scope">
      <PanelHead
        tile="tile-violet" icon={<IconNote />}
        title="Curate the journey" sub="Select your records. Refine the order. Connect the whole set."
      />
      {!plan && (
        <>
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
            <select value={beats}
              onChange={e => setBeats(e.target.value === 'auto' ? 'auto' : parseInt(e.target.value))}
              className="slot-select" style={{ maxWidth: 170, marginBottom: 0 }}>
              <option value="auto">Auto-length blends</option>
              {[16, 32, 64].map(n => <option key={n} value={n}>{n}-beat blends</option>)}
            </select>
            {chosen.length >= 2 && (
              <LiquidMetalButton label="Plan the set" width={160} onClick={makePlan} disabled={busy} />
            )}
          </div>
        </>
      )}

      {plan && (
        <>
          <div className="setlist" style={{ maxHeight: 'none' }}>
            {plan.tracks.map((t, i) => (
              <div key={t.file}>
                <div className="set-row on">
                  <span className="set-order-btns">
                    <button className="chip tiny" disabled={i === 0} onClick={() => move(i, -1)}>↑</button>
                    <button className="chip tiny" disabled={i === plan.tracks.length - 1}
                      onClick={() => move(i, 1)}>↓</button>
                  </span>
                  <span className="set-name">{t.name}</span>
                  <span className="facts">{Math.round(t.bpm)} BPM{t.camelot ? ` · ${t.camelot}` : ''}</span>
                </div>
                {i < plan.tracks.length - 1 && (
                  <div className="set-join">
                    <span className="set-join-line">↓</span>
                    <select className="slot-select" style={{ maxWidth: 170, marginBottom: 0 }}
                      value={plan.joins[i]?.style || 'automix'}
                      onChange={e => setJoinStyle(i, e.target.value)}>
                      {JOIN_STYLES.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
                    </select>
                    <span className="wave-hint">{plan.joins[i]?.note}</span>
                    <button className="chip tiny" onClick={() => previewJoin(i)}
                      disabled={joinPreview[i]?.busy}>
                      {joinPreview[i]?.busy ? 'rendering…' : 'Preview join'}
                    </button>
                    <button className={'chip tiny' + (joinEdit[i]?.open ? ' active' : '')}
                      onClick={() => toggleJoinEdit(i)} disabled={joinEdit[i]?.busy}>
                      {joinEdit[i]?.busy ? 'analyzing…' : joinEdit[i]?.open ? 'Close editor' : 'Edit join'}
                    </button>
                    {(joinOverrides(i).cut != null || joinOverrides(i).b_start != null) && (
                      <span className="wave-hint" style={{ color: 'var(--accent)' }}>edited</span>
                    )}
                  </div>
                )}
                {i < plan.tracks.length - 1 && joinEdit[i]?.open && (
                  <div className="join-editor">
                    {joinEdit[i].error && (
                      <div className="wave-hint" style={{ color: '#ff6961' }}>
                        Error: {joinEdit[i].error}
                      </div>
                    )}
                    {joinEdit[i].inspect && (
                      <>
                        <TransitionLane
                          src={plan.tracks[i].file} info={joinEdit[i].inspect.a}
                          marker={joinEdit[i].cut} mixBeats={beats}
                          onMarker={v => setJoinEdit(p => ({ ...p, [i]: { ...p[i], cut: v } }))}
                          label={`Exit — ${plan.tracks[i].name}`}
                          note={joinEdit[i].cut === joinEdit[i].inspect.a.cut
                            ? 'auto — drag to adjust' : 'manual'} />
                        {joinEdit[i].cut !== joinEdit[i].inspect.a.cut && (
                          <button className="chip tiny lane-reset"
                            onClick={() => setJoinEdit(p => ({ ...p, [i]: { ...p[i], cut: p[i].inspect.a.cut } }))}>
                            reset exit to auto
                          </button>
                        )}
                        <TransitionLane
                          src={plan.tracks[i + 1].file} info={joinEdit[i].inspect.b}
                          marker={joinEdit[i].bStart} mixBeats={beats}
                          onMarker={v => setJoinEdit(p => ({ ...p, [i]: { ...p[i], bStart: v } }))}
                          label={`Entry — ${plan.tracks[i + 1].name}`}
                          note={joinEdit[i].bStart === joinEdit[i].inspect.b.b_start
                            ? 'auto — drag to adjust' : 'manual'} />
                        {joinEdit[i].bStart !== joinEdit[i].inspect.b.b_start && (
                          <button className="chip tiny lane-reset"
                            onClick={() => setJoinEdit(p => ({ ...p, [i]: { ...p[i], bStart: p[i].inspect.b.b_start } }))}>
                            reset entry to auto
                          </button>
                        )}
                      </>
                    )}
                  </div>
                )}
                {i < plan.tracks.length - 1 && joinPreview[i]?.file && (
                  <div style={{ padding: '4px 12px 10px' }}>
                    {/* preview clips always place the transition at 12s */}
                    <Waveform src={encodeURI(joinPreview[i].file)} height={40} accent="#e8e8e8"
                      marks={[plan.joins[i]?.style === 'automix'
                        ? { start: 12, end: 12 + (beats === 'auto' ? 32 : beats) * 60 / (plan.tracks[i]?.bpm || 125) }
                        : { start: 12 - 2 * 60 / (plan.tracks[i]?.bpm || 125),
                            end: 12 + 2 * 60 / (plan.tracks[i]?.bpm || 125) }]} />
                  </div>
                )}
              </div>
            ))}
          </div>
          <div className="dj-controls">
            <button className="chip" onClick={() => { setPlan(null); setJoinPreview({}); setJoinEdit({}) }}>← Change tracks</button>
            <LiquidMetalButton label="Render the set" width={170} onClick={render} disabled={busy} />
          </div>
        </>
      )}

      <Status {...(status || {})} />
      {result && (
        <div className="results">
          <MediaCard title={decodeURIComponent(result.file.split('/').pop())}
            url={result.file} accent="#e8e8e8" marks={setMarks()} />
          <div className="media-card glass-soft">
            <div className="media-row">
              <span className="media-name">Tracklist</span>
              <span className="media-actions">
                <button className="chip" onClick={copyTracklist}>{copied ? 'Copied!' : 'Copy tracklist'}</button>
                <button className="chip" onClick={downloadCue}>Download .cue</button>
              </span>
            </div>
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
