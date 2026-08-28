import { useEffect, useState } from 'react'
import { postJSON } from '../api'
import { PanelHead, TrackFacts, IconNote } from '../ui'
import { Waveform, fmtTime } from '../Waveform'

const KINDS = [
  ['all', 'All'], ['downloads', 'Downloads'], ['stems', 'Stems'],
  ['mixes', 'DJ mixes'], ['karaoke', 'Karaoke'], ['converted', 'Converted'], ['midi', 'MIDI'],
]

function fmtSize(b) {
  return b > 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.round(b / 1024) + ' KB'
}

function fmtDate(ts) {
  return new Date(ts * 1000).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
}

export function LibraryView() {
  const [items, setItems] = useState([])
  const [kind, setKind] = useState('all')
  const [q, setQ] = useState('')
  const [open, setOpen] = useState(null)      // file url of expanded row
  const [recog, setRecog] = useState({})      // file -> {busy|result|error}

  const load = () => fetch('/library').then(r => r.json()).then(d => setItems(d.items || [])).catch(() => {})
  useEffect(() => { load() }, [])

  const shown = items.filter(it =>
    (kind === 'all' || it.kind === kind) &&
    (!q || it.name.toLowerCase().includes(q.toLowerCase())))

  async function del(it) {
    await postJSON('/library/delete', { file: decodeURI(it.file) }).catch(() => {})
    if (open === it.file) setOpen(null)
    load()
  }

  async function recognize(it) {
    setRecog(r => ({ ...r, [it.file]: { busy: true } }))
    try {
      const d = await postJSON('/recognize', { file: decodeURI(it.file) })
      setRecog(r => ({ ...r, [it.file]: d }))
    } catch (e) {
      setRecog(r => ({ ...r, [it.file]: { error: e.message } }))
    }
  }

  async function applyName(it, name) {
    try {
      await postJSON('/library/rename', { file: decodeURI(it.file), name })
      setRecog(r => ({ ...r, [it.file]: undefined }))
      setOpen(null)
      load()
    } catch (e) {
      setRecog(r => ({ ...r, [it.file]: { error: e.message } }))
    }
  }

  return (
    <div className="glass metal-scope">
      <PanelHead
        tile="tile-mint" icon={<IconNote />}
        title="Library" sub="Everything you've made, in one place"
      />
      <div className="lib-bar">
        <input className="input" style={{ maxWidth: 320, padding: '10px 16px' }}
          placeholder="search…" value={q} onChange={e => setQ(e.target.value)} />
        <div className="lib-kinds">
          {KINDS.map(([id, label]) => (
            <button key={id} className={'chip tiny' + (kind === id ? ' active' : '')}
              onClick={() => setKind(id)}>{label}</button>
          ))}
        </div>
      </div>

      <div className="lib-list">
        {shown.map(it => {
          const rec = recog[it.file]
          return (
            <div key={it.file} className="lib-row-wrap">
              <div className={'lib-row' + (open === it.file ? ' open' : '')}
                onClick={() => setOpen(open === it.file ? null : it.file)}>
                <span className="lib-kind">{it.kind}</span>
                <span className="lib-name">{it.name}</span>
                <span className="lib-meta">{fmtSize(it.size)} · {fmtDate(it.mtime)}</span>
              </div>
              {open === it.file && (
                <div className="lib-detail">
                  {it.video ? (
                    <video controls src={encodeURI(it.file)} />
                  ) : it.midi ? (
                    <div className="wave-hint">MIDI file — download and drop it into a DAW</div>
                  ) : (
                    <>
                      <div style={{ marginBottom: 10 }}><TrackFacts url={it.file} /></div>
                      <Waveform src={encodeURI(it.file)} height={56} accent="#e8e8e8" />
                    </>
                  )}
                  <div className="lib-actions">
                    <a className="chip" href={encodeURI(it.file)} download={it.name}>Download</a>
                    {it.kind === 'downloads' && !it.video && (
                      <button className="chip" onClick={e => { e.stopPropagation(); recognize(it) }}
                        disabled={rec?.busy}>
                        {rec?.busy ? 'listening…' : 'Recognize song'}
                      </button>
                    )}
                    <button className="chip" style={{ color: '#ff8a8a' }}
                      onClick={e => { e.stopPropagation(); del(it) }}>Delete</button>
                  </div>
                  {rec && !rec.busy && (
                    <div className="lib-recog">
                      {rec.error && <span className="wave-hint" style={{ color: '#ff8a8a' }}>{rec.error}</span>}
                      {rec.found === false && <span className="wave-hint">no match found</span>}
                      {rec.found && (
                        <>
                          <span>♪ {rec.artist} — {rec.title} <span className="wave-hint">(match {Math.round(rec.score * 100)}%)</span></span>
                          <button className="chip tiny" onClick={() => applyName(it, rec.suggested)}>
                            Rename file to this
                          </button>
                        </>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          )
        })}
        {!shown.length && <div className="wave-hint" style={{ padding: 20 }}>nothing here yet</div>}
      </div>
    </div>
  )
}
