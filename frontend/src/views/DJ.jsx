import { useEffect, useRef, useState } from 'react'
import { postForm, postJSON } from '../api'
import { Status, MediaCard, TrackFacts } from '../ui'
import { LiquidMetalButton } from '../LiquidMetalButton'
import { Waveform, fmtTime } from '../Waveform'
import { TransitionLane } from '../TransitionLane'
import { AudioLines, Plus, Disc3, Layers, Waves } from 'lucide-react'

const STYLES = [
  { id: 'auto', name: 'Auto direction', desc: 'Find a musical handover in your chosen mode. Your ears decide the winner.' },
  { id: 'automix', name: 'AutoMix', desc: 'compares phrase-aligned transitions without forcing incompatible passages together' },
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

function Deck({ label, hint, value, onChange, files, refresh, disabled }) {
  const inputRef = useRef(null)
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
    <div className={'deck' + (value ? ' is-loaded' : '')}>
      <div className="deck-aura" aria-hidden="true" /><div className="deck-label"><span className="deck-badge"><Disc3 size={15} /> {label}</span><span>{hint}</span></div>
      <div className="deck-identity"><div className="deck-channel" aria-hidden="true">{label.endsWith('A') ? 'A' : 'B'}</div><div className="deck-track"><small>{value ? 'READY ON DECK' : 'START HERE'}</small><strong title={value ? decodeURI(value.split('/').pop()) : undefined}>{value ? decodeURI(value.split('/').pop()).replace(/\.[^.]+$/, '') : label.endsWith('A') ? 'The first track.' : 'What comes next?'}</strong></div></div>
      <div className="deck-row">
        <select className="slot-select" style={{ marginBottom: 0 }}
          aria-label={`${label} source track`} disabled={disabled} value={value || ''} onChange={e => onChange(e.target.value || null)}>
          <option value="">choose a track…</option>
          {files.map(f => <option key={f.file} value={f.file}>{f.name}</option>)}
        </select>
        <button className="chip" disabled={disabled || uploading} onClick={() => inputRef.current.click()}>
          <Plus size={15} /> {uploading ? 'Loading…' : 'Upload'}
        </button>
        <input ref={inputRef} type="file" hidden accept="audio/*,.mp3,.wav,.flac,.m4a,.ogg,.aac"
          onChange={e => { if (e.target.files[0]) upload(e.target.files[0]); e.target.value = '' }} />
      </div>
      {value && <div className="deck-facts"><TrackFacts url={value} /></div>}
    </div>
  )
}

export function DJView({ handoff }) {
  const [files, setFiles] = useState([])
  const [a, setA] = useState(null)
  const [b, setB] = useState(null)
  const [suggest, setSuggest] = useState(null)   // {busy,stage,pct} | {matches,seed} | {error}
  const suggestPollRef = useRef(null)
  const [webSuggest, setWebSuggest] = useState(null)  // same shape, matches from the web
  const webPollRef = useRef(null)
  const [dl, setDl] = useState({})               // suggestion query -> busy|done|error:…
  const [rated, setRated] = useState({})         // candidate file -> 1 | -1
  const [evalInfo, setEvalInfo] = useState(null) // how well the ranking matches your taste
  const [style, setStyle] = useState('auto')
  const [mode, setMode] = useState('natural')
  const [referenceId, setReferenceId] = useState(null)
  const [learning, setLearning] = useState(null)
  const [beats, setBeats] = useState('auto')
  const [status, setStatus] = useState(null)
  const [result, setResult] = useState(null)
  const [previews, setPreviews] = useState(null)
  const [blindCompare, setBlindCompare] = useState(true)
  const [busy, setBusy] = useState(false)
  const [inspect, setInspect] = useState(null)
  const [cutSec, setCutSec] = useState(null)
  const [bStartSec, setBStartSec] = useState(null)
  const pollRef = useRef(null)

  const refresh = () => fetch('/files').then(r => r.json()).then(d => setFiles(d.files || [])).catch(() => {})
  useEffect(() => { refresh() }, [])
  useEffect(() => () => {
    clearTimeout(pollRef.current); clearTimeout(suggestPollRef.current); clearTimeout(webPollRef.current)
  }, [])

  // hand-off from Library: "find a mix partner" loads Deck A and scans
  useEffect(() => {
    if (handoff?.file) { setA(handoff.file); findMatches(handoff.file) }
  }, [handoff])

  async function findMatches(file = a) {
    if (!file) return
    setSuggest({ busy: true, stage: 'Starting…' })
    try {
      const d = await postJSON('/dj/suggest', { file: decodeURI(file) })
      pollSuggest(d.job)
    } catch (e) { setSuggest({ error: e.message }) }
  }

  async function pollSuggest(id) {
    try {
      const j = await (await fetch('/dj/status/' + id)).json()
      if (j.error) throw new Error(j.error)
      if (!j.done) {
        setSuggest({ busy: true, stage: j.stage, pct: j.pct })
        suggestPollRef.current = setTimeout(() => pollSuggest(id), 800)
        return
      }
      setSuggest({ matches: j.matches || [], seed: j.seed })
      setRated({})
      loadEval()
    } catch (e) { setSuggest({ error: e.message }) }
  }

  const loadEval = () => fetch('/dj/suggest/eval').then(r => r.json())
    .then(setEvalInfo).catch(() => {})

  // "would I actually mix these?" — the labels that let ranking changes be
  // measured instead of guessed at
  async function rate(m, verdict) {
    const seedFile = suggest?.seed?.file || (a && decodeURI(a))
    if (!seedFile) return
    const key = m.file || `web:${m.id}`
    setRated(r => ({ ...r, [key]: verdict }))
    try {
      await postJSON('/dj/rate', m.file
        ? { seed_file: decodeURI(seedFile), file: decodeURI(m.file), verdict }
        : {
          seed_file: decodeURI(seedFile), web_id: m.id, preview: m.preview,
          name: `${m.artist} — ${m.title}`, score: m.match, verdict,
        })
      loadEval()
    } catch { /* keep the local mark; the next rating retries */ }
  }

  async function findWebMatches(file = a) {
    if (!file) return
    setWebSuggest({ busy: true, stage: 'Starting…' })
    try {
      const d = await postJSON('/dj/suggest/web', { file: decodeURI(file) })
      pollWeb(d.job)
    } catch (e) { setWebSuggest({ error: e.message }) }
  }

  async function pollWeb(id) {
    try {
      const j = await (await fetch('/dj/status/' + id)).json()
      if (j.error) throw new Error(j.error)
      if (!j.done) {
        setWebSuggest({ busy: true, stage: j.stage, pct: j.pct })
        webPollRef.current = setTimeout(() => pollWeb(id), 800)
        return
      }
      setWebSuggest({ matches: j.web_matches || [], seed: j.seed })
    } catch (e) { setWebSuggest({ error: e.message }) }
  }

  async function grabToB(m) {
    setDl(d => ({ ...d, [m.query]: 'busy' }))
    try {
      const r = await postJSON('/yt', { url: 'ytsearch1:' + m.query, kind: 'audio', abr: 320 })
      setDl(d => ({ ...d, [m.query]: 'done' }))
      refresh()
      setB(r.file)
    } catch (e) {
      setDl(d => ({ ...d, [m.query]: 'error: ' + e.message }))
    }
  }

  useEffect(() => {
    setInspect(null); setCutSec(null); setBStartSec(null)
    setPreviews(null); setReferenceId(null)
    if (!a || !b) return
    let cancelled = false
    setStatus({ busy: true, text: 'Analyzing both tracks…' })
    postJSON('/dj/inspect', { a_file: decodeURI(a), b_file: decodeURI(b), beats, style, mode })
      .then(d => {
        if (cancelled) return
        setInspect(d)
        setCutSec(d.a.cut)
        setBStartSec(d.b.b_start)
        setStatus(null)
      })
      .catch(e => { if (!cancelled) setStatus({ text: 'Error: ' + e.message, error: true }) })
    return () => { cancelled = true }
  }, [a, b, beats, style, mode])

  async function start(preview = false, candidate = null) {
    if (!a || !b || busy || !inspect) return
    setBusy(true); setResult(null)
    if (!candidate) setPreviews(null)
    setStatus({ busy: true, text: 'Starting…' })
    try {
      const body = { a_file: decodeURI(a), b_file: decodeURI(b), style, beats, mode }
      if (candidate) body.candidate = candidate
      if (preview) body.preview = true
      if (cutSec != null && cutSec !== inspect?.a.cut) body.cut = cutSec
      if (bStartSec != null && bStartSec !== inspect?.b.b_start) body.b_start = bStartSec
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
        setStatus({ text: j.stage +
          (j.candidates_evaluated != null ? ` ${j.candidates_evaluated} rendered versions checked; ${j.candidates_accepted} passed.` : '') +
          (j.structure_analysis?.every(s => s === 'neural') ? ' Neural phrase maps used for both tracks.' : '') +
          (j.fallback ? ` ${j.fallback}.` : '') +
          (j.ranking_source ? ` Ranking: ${j.ranking_source}.` : '') +
          (j.analysis_notes?.length ? ` ${[...new Set(j.analysis_notes)].join('; ')}.` : '') })
        setPreviews(j.previews)
        setBusy(false)
        return
      }
      setStatus({
        text: `Done!` +
          (j.style_chosen ? ` Auto chose ${j.style_chosen} (${j.auto_reason}).` : '') +
          ` Transition at ${fmtTime(j.transition_at)}` +
          (j.b_skip > 0.5 ? ` · B enters from ${fmtTime(j.b_skip)}` : '') +
          (j.entry_plan && j.entry_plan !== 'manual' ? ` (${j.entry_plan})` : '') +
          (j.beats_used ? ` · ${j.beats_used}-beat blend${j.length_reason ? ` (${j.length_reason})` : ''}` : '') +
          (j.key_action ? ` · ${j.key_action}` : '') +
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
  const [voted, setVoted] = useState({})

  // green zone on the finished mix: the blend window for smooth styles,
  // a tight window around the switch for hard ones
  const HARD = ['tapestop', 'looproll', 'backspin', 'riser', 'cut', 'echo']
  // when length is on auto, inspect reports the engine's chosen beat count
  const shownBeats = beats === 'auto' ? (inspect?.beats || 32) : beats
  function mixMarks(at, styleUsed, duration) {
    const beat = 60 / (inspect?.a?.bpm || 125)
    return HARD.includes(styleUsed)
      ? [{ start: at - 2 * beat, end: at + 2 * beat }]
      : [{ start: at, end: at + (duration ?? Math.max(2, shownBeats * beat)) }]
  }
  function resultMarks() {
    if (!result || result.transition_at == null) return undefined
    return mixMarks(result.transition_at, result.style_used || result.style_chosen || style, result.transition_duration)
  }

  function vote(st, verdict, candidate = null) {
    setVoted(v => ({ ...v, [candidate || st]: verdict }))
    postJSON('/dj/feedback', {
      style: st, verdict, candidate, a_file: decodeURI(a || ''), b_file: decodeURI(b || ''), beats,
    }).then(r => setLearning(r.learning)).catch(e => setStatus({text: e.message, error: true}))
  }

  async function saveReference(candidate) {
    try {
      await postJSON('/dj/reference', {candidate, a_file: decodeURI(a), b_file: decodeURI(b)})
      setReferenceId(candidate)
    } catch (e) { setStatus({text: e.message, error: true}) }
  }

  return (
    <div className="glass metal-scope dj-console">
      <div className="dj-decks">
        <Deck label="Deck A" hint="OUTGOING" value={a} onChange={setA}
          files={files} refresh={refresh} disabled={busy} />
        <Deck label="Deck B" hint="INCOMING" value={b} onChange={setB}
          files={files} refresh={refresh} disabled={busy} />
      </div>
      {(!a || !b) && <div className="mix-empty"><AudioLines size={25} /><div><strong>{a || b ? 'One more track to go.' : 'Your transition, visualized.'}</strong><p>Load both decks to unlock waveforms and phrase-by-phrase editing.</p></div><span className="waiting-label">Waiting for audio</span></div>}

      {a && (
        <div className="dj-suggest">
          <button className="chip" onClick={() => findMatches()} disabled={suggest?.busy}>
            {suggest?.busy ? 'Finding connections…' : 'Find a matching record'}
          </button>
          <button className="chip" onClick={() => findWebMatches()} disabled={webSuggest?.busy}>
            {webSuggest?.busy ? 'Searching…' : 'Explore online'}
          </button>
          {suggest?.busy && <span className="wave-hint">{suggest.stage}{suggest.pct != null ? ` ${Math.round(suggest.pct)}%` : ''}</span>}
          {suggest?.error && <span className="wave-hint" style={{ color: '#ff8a8a' }}>{suggest.error}</span>}
          {suggest?.seed && !suggest?.busy && <span className="wave-hint">best partners for “{suggest.seed.name}” — click one to load Deck B</span>}
          {webSuggest?.busy && <span className="wave-hint">{webSuggest.stage}{webSuggest.pct != null ? ` ${Math.round(webSuggest.pct)}%` : ''}</span>}
          {webSuggest?.error && <span className="wave-hint" style={{ color: '#ff8a8a' }}>{webSuggest.error}</span>}
        </div>
      )}
      {a && suggest?.matches && (
        <div className="results suggest-list">
          {suggest.matches.map(m => (
            <div key={m.file}
              className={'media-card glass-soft suggest-row' + (b === m.file ? ' picked' : '')}
              onClick={() => setB(m.file)}>
              <div className="media-row">
                <span className="suggest-pct">{m.match}%</span>
                <span className="media-name">{m.name}</span>
                <span className="media-actions">
                  <button className={'chip tiny' + (rated[m.file] === 1 ? ' active' : '')}
                    title="I would mix these"
                    onClick={e => { e.stopPropagation(); rate(m, 1) }}>👍</button>
                  <button className={'chip tiny' + (rated[m.file] === -1 ? ' active' : '')}
                    title="Bad pairing"
                    onClick={e => { e.stopPropagation(); rate(m, -1) }}>👎</button>
                  <button className="chip tiny">{b === m.file ? 'on Deck B ✓' : 'Use as Deck B'}</button>
                </span>
              </div>
              <div className="wave-hint suggest-why">
                {m.bpm ? `${m.bpm} BPM` : 'BPM ?'}{m.camelot ? ` · ${m.camelot}` : ''} · {m.reasons.join(' · ')} · style: {m.style}
              </div>
            </div>
          ))}
          {!suggest.matches.length && <div className="wave-hint" style={{ padding: 12 }}>no other tracks to match — download a few more first</div>}
          {!!suggest.matches.length && (
            <div className="suggest-eval wave-hint">
              👍/👎 each pair — that&apos;s what makes the ranking measurable.
              {evalInfo?.verdict && <><br />{evalInfo.verdict}</>}
              {evalInfo?.auc != null && (
                <> {' '}Liked pairs average {evalInfo.mean_score_liked}%, disliked {evalInfo.mean_score_disliked}%.</>
              )}
            </div>
          )}
        </div>
      )}
      {a && webSuggest?.matches && (
        <div className="results suggest-list">
          {webSuggest.seed && (
            <div className="wave-hint" style={{ marginTop: 10 }}>
              from the web, for “{webSuggest.seed.artist} — {webSuggest.seed.title}” ·
              BPM and key measured from each 30s preview · listen, then 👍/👎 or download onto Deck B
            </div>
          )}
          {webSuggest.matches.map(m => (
            <div key={m.query} className="media-card glass-soft">
              <div className="media-row">
                <span className="suggest-pct">{m.match}%</span>
                {m.cover && <img className="suggest-cover" src={m.cover} alt="" />}
                <span className="media-name">{m.artist} — {m.title}</span>
                <span className="media-actions">
                  {m.preview && <audio className="suggest-preview" controls preload="none" src={m.preview} />}
                  <button className={'chip tiny' + (rated['web:' + m.id] === 1 ? ' active' : '')}
                    title="I would mix these" onClick={() => rate(m, 1)}>👍</button>
                  <button className={'chip tiny' + (rated['web:' + m.id] === -1 ? ' active' : '')}
                    title="Bad pairing" onClick={() => rate(m, -1)}>👎</button>
                  <button className="chip tiny" disabled={dl[m.query] === 'busy'} onClick={() => grabToB(m)}>
                    {dl[m.query] === 'busy' ? 'downloading…' : dl[m.query] === 'done' ? 'on Deck B ✓' : 'Download → Deck B'}
                  </button>
                </span>
              </div>
              <div className="wave-hint suggest-why">
                {m.bpm ? `${Math.round(m.bpm)} BPM · ` : ''}{m.camelot ? `${m.camelot} · ` : ''}
                {m.reasons.join(' · ')}{m.style ? ` · style: ${m.style}` : ''}
                {dl[m.query]?.startsWith?.('error') ? ` · ${dl[m.query]}` : ''}
              </div>
            </div>
          ))}
          {!webSuggest.matches.length && <div className="wave-hint" style={{ padding: 12 }}>nothing new found — you may already have the best pairings</div>}
          {!!webSuggest.matches.length && (
            <div className="suggest-eval wave-hint">
              👍/👎 these too — the web pool is far bigger than your library, so it&apos;s the better place to build up ratings.
              {evalInfo?.verdict && <><br />{evalInfo.verdict}</>}
              {evalInfo?.web?.auc != null && <> Web-only AUC {evalInfo.web.auc.toFixed(2)} over {evalInfo.web.n}.</>}
            </div>
          )}
        </div>
      )}

      {inspect && (
        <div className="dj-editor">
          <TransitionLane
            src={a} info={inspect.a} marker={cutSec} onMarker={setCutSec} mixBeats={shownBeats}
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
            src={b} info={inspect.b} marker={bStartSec} onMarker={setBStartSec} mixBeats={shownBeats}
            label="Deck B — entry point"
            note={bStartSec === inspect.b.b_start ? `auto: ${inspect.entry_plan || 'provisional entry'}` : 'manual — drag to adjust'} />
          {bStartSec !== inspect.b.b_start && (
            <button className="chip tiny lane-reset" onClick={() => setBStartSec(inspect.b.b_start)}>
              reset to auto
            </button>
          )}
          <div className="wave-hint">
            green = where the tracks mix · brighter waveform = higher-energy section · lines = section boundaries · markers snap to bars
            {beats === 'auto' && inspect.beats &&
              <> · auto length: {inspect.beats} beats{inspect.length_reason ? ` — ${inspect.length_reason}` : ''}</>}
          </div>
        </div>
      )}

      {['auto', 'automix'].includes(style) && <div className="mode-section"><div className="mode-heading"><h3>Choose your approach.</h3><span>Three ways to connect your tracks.</span></div><div className="mode-options" role="radiogroup" aria-label="Mixing mode">{[
        ['natural', 'Natural', 'Keep the original sound.', <Waves size={21} />],
        ['club', 'Club', 'Longer blends. More movement.', <Disc3 size={21} />],
        ['creative', 'Creative', 'Rework the individual stems.', <Layers size={21} />],
      ].map(([id, name, desc, icon]) => <label key={id} className={'mode-card' + (mode === id ? ' selected' : '')}><input type="radio" name="mixing-mode" value={id} checked={mode === id} disabled={busy} onChange={() => setMode(id)} />{icon}<span><strong>{name}</strong><small>{desc}</small></span><i className="mode-dot" /></label>)}</div></div>}
      <div className="dj-controls">
        <select aria-label="Transition style" disabled={busy} value={style} onChange={e => setStyle(e.target.value)} className="slot-select"
          style={{ maxWidth: 260, marginBottom: 0 }}>
          {STYLES.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        <select aria-label="Transition length" disabled={busy} value={beats}
          onChange={e => setBeats(e.target.value === 'auto' ? 'auto' : parseInt(e.target.value))}
          className="slot-select" style={{ maxWidth: 130, marginBottom: 0 }}>
          <option value="auto">Auto length</option>
          {[4, 8, 16, 32, 64].map(n => <option key={n} value={n}>{n} beats</option>)}
        </select>
        {a && b && (
          <>
            <LiquidMetalButton label="Render your mix" width={170} onClick={() => start(false)} disabled={busy || !inspect} />
            <button className="chip" disabled={busy || !inspect} onClick={() => start(true)}>
              {['auto', 'automix'].includes(style) ? 'Audition transitions' : 'Audition styles'}
            </button>
          </>
        )}
      </div>
      {styleInfo && <div className="wave-hint" style={{ textAlign: 'center', marginTop: 8 }}>{styleInfo.desc}</div>}
      {['auto', 'automix'].includes(style) && <div className="wave-hint" style={{textAlign: 'center', marginTop: 8}}>
        {mode === 'creative' ? 'Creative processing may alter the recording. Loop rolls and other effects remain separate styles.'
          : 'Stems are used only for analysis. Uncertain timing or unfinished vocal lines lead to a native-tempo handover.'}
      </div>}

      <Status {...(status || {})} />
      {previews && (
        <div className="results">
          {previews.some(p => p.candidate) && <div className="wave-hint">
            Listen to each version, then export your favorite. Export keeps this exact transition.
            <button className="chip tiny" onClick={() => setBlindCompare(v => !v)}>
              {blindCompare ? 'Show mixing methods' : 'Hide names for A/B comparison'}
            </button>
          </div>}
          {previews.map((p, i) => p.error ? (
            <div key={p.candidate || p.style} className="media-card glass-soft">
              <div className="media-row"><span className="media-name">{p.style}</span>
                <span style={{ color: '#ff6961', fontSize: '0.82rem' }}>{p.error}</span></div>
            </div>
          ) : (
            <div key={p.candidate || p.style} className="media-card glass-soft">
              <div className="media-row">
                <span className="media-name">{p.candidate
                  ? (p.reference ? p.name : blindCompare ? `Version ${String.fromCharCode(65 + i)}` : `${p.name}${p.recommended ? ' · first diagnostic candidate' : ''}`)
                  : STYLES.find(s => s.id === p.style)?.name || p.style}</span>
                <span className="media-actions">
                  <button className={'chip tiny' + (voted[p.candidate || p.style] === 1 ? ' active' : '')}
                    onClick={() => vote(p.style, 1, p.candidate)}>👍</button>
                  <button className={'chip tiny' + (voted[p.candidate || p.style] === -1 ? ' active' : '')}
                    onClick={() => vote(p.style, -1, p.candidate)}>👎</button>
                  <button className="chip" disabled={busy} onClick={() => p.candidate ? start(false, p.candidate) : setStyle(p.style)}>
                    {p.candidate ? 'Export this transition' : 'Use this style'}
                  </button>
                  {p.candidate && <button className="chip tiny" disabled={busy}
                    onClick={() => saveReference(p.candidate)}>
                    {referenceId === p.candidate ? 'Reference saved ✓' : 'Set listening reference'}
                  </button>}
                </span>
              </div>
              {p.fallback && <div className="wave-hint">{p.fallback}</div>}
              <Waveform src={encodeURI(p.file)} height={48} accent="#e8e8e8"
                marks={mixMarks(p.transition_at ?? 12, p.style_used || p.style, p.transition_duration)} />
            </div>
          ))}
          {learning && <div className="wave-hint">
            {learning.trained ? 'Listening preference model validated on held-out track pairs.' : learning.reason}
            {learning.ratings != null ? ` · ${learning.ratings} rated transitions across ${learning.pairs} pairs` : ''}
          </div>}
        </div>
      )}
      {result && (
        <div className="results">
          <MediaCard title={decodeURIComponent(result.file.split('/').pop())}
            url={result.file} accent="#e8e8e8" marks={resultMarks()} />
        </div>
      )}
    </div>
  )
}
