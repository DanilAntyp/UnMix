import { useState } from 'react'
import { postForm } from '../api'
import { DropZone, Status, MediaCard, PanelHead, IconScissors } from '../ui'

export function ConvertView() {
  const [file, setFile] = useState(null)
  const [fmt, setFmt] = useState('mp3-192')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [status, setStatus] = useState(null)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)

  function pick(f) {
    setFile(f); setResult(null); setStatus(null)
  }

  async function run() {
    if (!file || busy) return
    setBusy(true); setResult(null)
    setStatus({ busy: true, text: 'Converting…' })
    try {
      const fd = new FormData()
      fd.append('audio', file)
      fd.append('format', fmt)
      fd.append('start', start.trim())
      fd.append('end', end.trim())
      const data = await postForm('/convert', fd)
      setStatus({ text: 'Done!' })
      setResult(data)
    } catch (e) {
      setStatus({ text: 'Error: ' + e.message, error: true })
    } finally { setBusy(false) }
  }

  return (
    <div className="glass">
      <PanelHead
        tile="tile-mint" icon={<IconScissors />}
        title="Convert & Trim" sub="Change format or cut a piece out of any audio or video file"
      />
      <DropZone onFile={pick} icon={<IconScissors width={30} height={30} color="#9a9aa8" />}
        hint="any audio or video file" />
      {file && <div className="picked">♪ {file.name}</div>}

      <div className="setting">
        <label>Format:</label>
        <select value={fmt} onChange={e => setFmt(e.target.value)}>
          <option value="mp3-320">mp3 — 320k</option>
          <option value="mp3-192">mp3 — 192k</option>
          <option value="mp3-128">mp3 — 128k</option>
          <option value="wav">wav</option>
          <option value="flac">flac</option>
          <option value="m4a">m4a</option>
        </select>
      </div>
      <div className="setting">
        <label>Trim:</label>
        <input type="text" value={start} placeholder="start mm:ss (optional)"
          onChange={e => setStart(e.target.value)} />
        <input type="text" value={end} placeholder="end mm:ss (optional)"
          onChange={e => setEnd(e.target.value)} />
      </div>

      {file && (
        <div style={{ marginTop: 16 }}>
          <button className="btn-primary wide" onClick={run} disabled={busy}>Convert</button>
        </div>
      )}
      <Status {...(status || {})} />

      {result && (
        <div className="results">
          <MediaCard title={result.name} url={result.file} />
        </div>
      )}
    </div>
  )
}
