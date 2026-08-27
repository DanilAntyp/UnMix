import { useEffect, useRef, useState } from 'react'
import Panel from '../components/Panel'
import Icon from '../components/Icon'
import Status from '../components/Status'
import Track from '../components/Track'
import DropZone from '../components/DropZone'
import { Field, Select } from '../components/Field'
import { postForm, postJSON, pollProgress } from '../api'
import { STEMS, MODEL_STEMS, stemLabel } from '../stems'

const AUDIO_ACCEPT = 'audio/*,.mp3,.wav,.flac,.m4a,.ogg,.aac'

const MODEL_OPTIONS = [
  { value: 'htdemucs', label: 'Standard — 4 stems, best quality' },
  { value: 'htdemucs_6s', label: 'Extended — 6 stems, adds guitar & piano' },
]

export default function SeparatePanel({ request }) {
  const [model, setModel] = useState('htdemucs')
  const [stem, setStem] = useState('vocals')
  const [status, setStatus] = useState(null)
  const [result, setResult] = useState(null)
  const box = useRef(null)

  // Keep the stem choice valid when the model changes (guitar/piano are 6s only).
  const stems = MODEL_STEMS[model]
  useEffect(() => {
    if (stem !== 'all' && !stems.includes(stem)) setStem('vocals')
  }, [model]) // eslint-disable-line react-hooks/exhaustive-deps

  const stemOptions = [
    ...stems.map(k => ({
      value: k,
      label: stemLabel(k) + (k === 'other' ? ' (synths, strings, …)' : ''),
    })),
    { value: 'all', label: `✨ All instruments — ${stems.length} separate files` },
  ]

  async function run(label, send) {
    setResult(null)
    setStatus({ kind: 'busy', text: `Separating “${label}”…`, pct: 0 })
    const stop = pollProgress('/progress/sep', pct =>
      setStatus({ kind: 'busy', text: `Separating “${label}”… ${Math.round(pct)}%`, pct }))
    try {
      const data = await send()
      setResult(data)
      setStatus({ kind: 'done', text: 'Done — stems saved to separated/' })
    } catch (err) {
      setStatus({ kind: 'error', text: err.message })
    } finally {
      stop()
    }
  }

  const uploadSong = file =>
    run(file.name, () => postForm('/separate', { audio: file, remove: stem, model }))

  // "Split" in the YouTube panel hands a already-downloaded file over here.
  useEffect(() => {
    if (!request) return
    box.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    run(request.title, () =>
      postJSON('/separate', { server_file: request.file, remove: stem, model }))
  }, [request]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div ref={box}>
      <Panel
        mark={<Icon name="bolt" fill="currentColor" stroke={0} />}
        title="Sound Extraction"
        desc="Drop a song — get the chosen sound isolated, plus the track without it."
        accent="#8b5cff" accent2="#22e4ff"
      >
        <DropZone
          onFile={uploadSong}
          accept={AUDIO_ACCEPT}
          lead="Drop a song here"
          hint="or click to choose · mp3, wav, flac, m4a…"
        />

        <Field label="Model">
          <Select value={model} onChange={setModel} options={MODEL_OPTIONS} />
        </Field>
        <Field label="Sound to extract">
          <Select value={stem} onChange={setStem} options={stemOptions} />
        </Field>

        <Status state={status} />

        {result && resultTracks(result).map(([key, label, url]) => (
          <Track key={key} name={label} url={url} dot={STEMS[key]?.color || '#8b5cff'} />
        ))}
      </Panel>
    </div>
  )
}

/** Flatten either shape the /separate endpoint returns into [key, label, url]. */
function resultTracks(data) {
  if (data.remove === 'all') {
    return Object.keys(STEMS)
      .filter(k => data.all[k])
      .map(k => [k, stemLabel(k), data.all[k]])
  }
  return [
    [data.remove, stemLabel(data.remove), data.target],
    ['rest', '\u{1F3B5} Everything else', data.rest],
  ]
}
