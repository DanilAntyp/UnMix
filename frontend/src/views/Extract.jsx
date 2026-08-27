import { useEffect, useState } from 'react'
import { postForm, postJSON, pollProgress, STEM_NAMES, MODEL_STEMS } from '../api'
import { DropZone, Status, MediaCard, PanelHead, IconWave } from '../ui'

export function ExtractView({ handoff }) {
  const [model, setModel] = useState('htdemucs')
  const [stem, setStem] = useState('vocals')
  const [picked, setPicked] = useState(null)      // {file} local File or {server, title}
  const [status, setStatus] = useState(null)
  const [results, setResults] = useState([])      // [{title, url}]
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (handoff) {
      setPicked({ server: handoff.file, title: handoff.title })
      setResults([]); setStatus(null)
    }
  }, [handoff])

  const stems = MODEL_STEMS[model]
  useEffect(() => {
    if (stem !== 'all' && !stems.includes(stem)) setStem('vocals')
  }, [model])

  function pickLocal(f) {
    setPicked({ file: f, title: f.name })
    setResults([]); setStatus(null)
  }

  async function run() {
    if (!picked) return
    setBusy(true); setResults([])
    setStatus({ busy: true, text: `Separating “${picked.title}”…`, pct: 0 })
    const stop = pollProgress('/progress/sep', pct =>
      setStatus({ busy: true, text: `Separating “${picked.title}”…`, pct }))
    try {
      let data
      if (picked.file) {
        const fd = new FormData()
        fd.append('audio', picked.file)
        fd.append('remove', stem)
        fd.append('model', model)
        data = await postForm('/separate', fd)
      } else {
        data = await postJSON('/separate', { server_file: picked.server, remove: stem, model })
      }
      stop()
      setStatus({ text: 'Done!' })
      if (data.remove === 'all') {
        setResults(Object.keys(STEM_NAMES).filter(k => data.all[k])
          .map(k => ({ title: STEM_NAMES[k], url: data.all[k] })))
      } else {
        setResults([
          { title: STEM_NAMES[data.remove] || data.remove, url: data.target },
          { title: 'Everything else', url: data.rest },
        ])
      }
    } catch (e) {
      stop()
      setStatus({ text: 'Error: ' + e.message, error: true })
    } finally { setBusy(false) }
  }

  return (
    <div className="glass">
      <PanelHead
        tile="tile-sun" icon={<IconWave />}
        title="Sound Extraction" sub="Isolate any part of a song — or split everything"
      />
      <DropZone onFile={pickLocal} icon={<IconWave width={30} height={30} color="#9a9aa8" />}
        hint="mp3, wav, flac, m4a …" accept="audio/*,.mp3,.wav,.flac,.m4a,.ogg,.aac" />
      {picked && <div className="picked">♪ {picked.title}</div>}

      <div className="setting">
        <label>Model:</label>
        <select value={model} onChange={e => setModel(e.target.value)}>
          <option value="htdemucs">Standard — 4 stems, best quality</option>
          <option value="htdemucs_6s">Extended — 6 stems, adds guitar & piano</option>
        </select>
      </div>
      <div className="setting">
        <label>Sound to extract:</label>
        <select value={stem} onChange={e => setStem(e.target.value)}>
          {stems.map(k => (
            <option key={k} value={k}>
              {STEM_NAMES[k]}{k === 'other' ? ' (synths, strings, …)' : ''}
            </option>
          ))}
          <option value="all">All instruments ({stems.length} separate files)</option>
        </select>
      </div>

      {picked && (
        <div style={{ marginTop: 16 }}>
          <button className="btn-primary wide" onClick={run} disabled={busy}>Extract</button>
        </div>
      )}
      <Status {...(status || {})} />

      {results.length > 0 && (
        <div className="results">
          {results.map(r => <MediaCard key={r.url} title={r.title} url={r.url} />)}
        </div>
      )}
    </div>
  )
}
