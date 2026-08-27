import { useState } from 'react'
import Panel from '../components/Panel'
import Icon from '../components/Icon'
import Status from '../components/Status'
import Track from '../components/Track'
import DropZone from '../components/DropZone'
import PickedFile from '../components/PickedFile'
import { Field, Select } from '../components/Field'
import { postForm } from '../api'

const FORMATS = [
  { value: 'mp3-320', label: 'mp3 — 320 kbps' },
  { value: 'mp3-192', label: 'mp3 — 192 kbps' },
  { value: 'mp3-128', label: 'mp3 — 128 kbps' },
  { value: 'wav', label: 'wav — uncompressed' },
  { value: 'flac', label: 'flac — lossless' },
  { value: 'm4a', label: 'm4a — aac 192 kbps' },
]

export default function ConvertPanel() {
  const [file, setFile] = useState(null)
  const [format, setFormat] = useState('mp3-192')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [status, setStatus] = useState(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)

  function pick(f) {
    setFile(f); setResult(null); setStatus(null)
  }

  async function convert() {
    if (!file) return
    setBusy(true); setResult(null)
    setStatus({ kind: 'busy', text: 'Converting…' })
    try {
      const data = await postForm('/convert', {
        audio: file, format, start: start.trim(), end: end.trim(),
      })
      setResult(data)
      setStatus({ kind: 'done', text: 'Done — saved to converted/' })
    } catch (err) {
      setStatus({ kind: 'error', text: err.message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Panel
      mark={<Icon name="scissors" />}
      title="Convert &amp; Trim"
      desc="Change the format, or cut a piece out of any audio or video file."
      accent="#ffc233" accent2="#ff7a1a"
    >
      <DropZone
        onFile={pick}
        accept="audio/*,video/*"
        icon={<Icon name="scissors" size={22} />}
        lead="Drop a file here"
        hint="or click to choose · any audio or video"
      />
      <PickedFile name={file?.name} />

      <Field label="Output format">
        <Select value={format} onChange={setFormat} options={FORMATS} />
      </Field>
      <Field label="Trim (optional)">
        <div className="row">
          <input type="text" value={start} placeholder="start · mm:ss"
                 onChange={e => setStart(e.target.value)} />
          <input type="text" value={end} placeholder="end · mm:ss"
                 onChange={e => setEnd(e.target.value)} />
        </div>
      </Field>

      <button className="btn block" style={{ marginTop: 16 }}
              onClick={convert} disabled={!file || busy}>
        Convert
      </button>

      <Status state={status} />
      {result && <Track name={result.name} url={result.file} />}
    </Panel>
  )
}
