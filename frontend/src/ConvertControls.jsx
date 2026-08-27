import { useEffect, useState } from 'react'
import { postJSON } from './api'
import { Status, MediaCard } from './ui'
import { fmtTimePrecise } from './Waveform'

/* Inline convert & trim controls for a file already on the server.
   `sel` (from the waveform selection) auto-fills the trim range. */
export function ConvertControls({ serverFile, sel }) {
  const [fmt, setFmt] = useState('mp3-192')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [status, setStatus] = useState(null)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (sel) {
      setStart(fmtTimePrecise(sel[0]))
      setEnd(fmtTimePrecise(sel[1]))
    } else {
      setStart(''); setEnd('')
    }
  }, [sel])

  useEffect(() => { setResult(null); setStatus(null) }, [serverFile])

  async function run() {
    setBusy(true); setResult(null)
    setStatus({ busy: true, text: 'Converting…' })
    try {
      const data = await postJSON('/convert', {
        server_file: serverFile, format: fmt, start: start.trim(), end: end.trim(),
      })
      setStatus({ text: 'Done!' })
      setResult(data)
    } catch (e) {
      setStatus({ text: 'Error: ' + e.message, error: true })
    } finally { setBusy(false) }
  }

  return (
    <div className="convert-inline">
      <div className="convert-row">
        <select value={fmt} onChange={e => setFmt(e.target.value)}>
          <option value="mp3-320">mp3 — 320k</option>
          <option value="mp3-192">mp3 — 192k</option>
          <option value="mp3-128">mp3 — 128k</option>
          <option value="wav">wav</option>
          <option value="flac">flac</option>
          <option value="m4a">m4a</option>
        </select>
        <input type="text" value={start} placeholder="start"
          onChange={e => setStart(e.target.value)} />
        <input type="text" value={end} placeholder="end"
          onChange={e => setEnd(e.target.value)} />
        <button className="chip grad" onClick={run} disabled={busy}>
          {start || end ? 'Trim & convert' : 'Convert'}
        </button>
      </div>
      <Status {...(status || {})} />
      {result && (
        <div className="results">
          <MediaCard title={result.name} url={result.file} />
        </div>
      )}
    </div>
  )
}
