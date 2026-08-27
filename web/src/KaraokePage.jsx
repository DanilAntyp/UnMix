import { useEffect, useRef, useState } from 'react'
import Backdrop from './components/Backdrop'
import DropZone from './components/DropZone'
import PickedFile from './components/PickedFile'
import Status from './components/Status'
import Track from './components/Track'
import { Check } from './components/Field'
import { getJSON, postForm, postJSON, fileName } from './api'
import { STEMS } from './stems'

const REMOVABLE = [
  { key: 'vocals', sub: '— the lead voice' },
  { key: 'drums', sub: '' },
  { key: 'bass', sub: '' },
  { key: 'other', sub: '— guitars, synths, …' },
]

/* Matched loosely against the stage string the server reports, so the ladder
   lights up without the backend having to know about the UI. */
const LADDER = [
  { match: 'lyric', label: 'Looking up lyrics' },
  { match: 'Separat', label: 'Separating instruments' },
  { match: 'Whisper', label: 'Timing the words' },
  { match: 'Render', label: 'Rendering the video' },
]

export default function KaraokePage() {
  const [file, setFile] = useState(null)
  const [serverFile, setServerFile] = useState(null)
  const [remove, setRemove] = useState({ vocals: true })
  const [status, setStatus] = useState(null)
  const [stage, setStage] = useState(null)
  const [busy, setBusy] = useState(false)
  const [video, setVideo] = useState(null)
  const timer = useRef(null)

  // Opened from the YouTube panel as /karaoke?file=/downloads/… — nothing to upload.
  useEffect(() => {
    const preload = new URLSearchParams(location.search).get('file')
    if (preload && preload.startsWith('/downloads/')) setServerFile(preload)
    return () => clearTimeout(timer.current)
  }, [])

  const name = file ? file.name : serverFile ? fileName(serverFile) : null
  const chosen = Object.keys(remove).filter(k => remove[k])

  function pick(f) {
    setFile(f); setServerFile(null); setVideo(null); setStatus(null); setStage(null)
  }

  async function start() {
    if (!name) return
    if (!chosen.length) {
      setStatus({ kind: 'error', text: 'Pick at least one thing to remove.' })
      return
    }
    setBusy(true); setVideo(null); setStage(null)
    setStatus({ kind: 'busy', text: 'Starting…' })
    try {
      const body = { remove: chosen.join(',') }
      const data = serverFile
        ? await postJSON('/karaoke/start', { ...body, server_file: serverFile })
        : await postForm('/karaoke/start', { ...body, audio: file })
      poll(data.job)
    } catch (err) {
      setStatus({ kind: 'error', text: err.message })
      setBusy(false)
    }
  }

  async function poll(job) {
    try {
      const j = await getJSON('/karaoke/status/' + job)
      if (j.error) throw new Error(j.error)
      setStage(j.stage)
      if (!j.done) {
        setStatus({ kind: 'busy', text: j.stage, pct: j.pct })
        timer.current = setTimeout(() => poll(job), 1500)
        return
      }
      setStatus({ kind: 'done', text: j.stage })
      setVideo(j.video)
      setBusy(false)
    } catch (err) {
      setStatus({ kind: 'error', text: err.message })
      setBusy(false)
    }
  }

  const stageIndex = LADDER.findIndex(s => (stage || '').includes(s.match))

  return (
    <>
      <Backdrop />
      <a className="back" href="/">&#8592; UnMix</a>

      <div className="karaoke-page">
        <header className="masthead">
          <span className="eyebrow"><span className="dot" /> Karaoke Mode</span>
          <h1 className="wordmark" style={{ fontSize: 'clamp(2.6rem, 9vw, 4.6rem)' }}>
            Sing it
          </h1>
          <p className="tagline">
            Pick what to mute and UnMix renders an <b>mp4</b> with the lyrics in sync —
            each word lighting up as it is sung.
          </p>
        </header>

        <section className="panel" style={{ '--accent': '#ff2e9a', '--accent-2': '#ffc233' }}>
          <DropZone
            onFile={pick}
            accept="audio/*,.mp3,.wav,.flac,.m4a,.ogg,.aac"
            lead="Drop a song here"
            hint="or click to choose"
          />
          <PickedFile name={name} />

          <div className="field">
            <span className="label">Remove from the audio</span>
            <div className="checks">
              {REMOVABLE.map(({ key, sub }) => (
                <Check
                  key={key}
                  checked={!!remove[key]}
                  onChange={on => setRemove(r => ({ ...r, [key]: on }))}
                  sub={sub}
                >
                  {STEMS[key].icon} {STEMS[key].label}
                </Check>
              ))}
            </div>
          </div>

          <button className="btn block lg" style={{ marginTop: 18 }}
                  onClick={start} disabled={busy || !name}>
            {name ? 'Create karaoke video' : 'Pick a song first'}
          </button>

          <Status state={status} />

          {busy && stageIndex >= 0 && (
            <div className="stage-list">
              {LADDER.map((s, i) => (
                <div key={s.label}
                     className={'stage-row' + (i === stageIndex ? ' now' : i < stageIndex ? ' past' : '')}>
                  <span className="pip" />
                  <span>{s.label}</span>
                </div>
              ))}
              <div className="stage-row" style={{ paddingTop: 10 }}>
                A full song takes a few minutes.
              </div>
            </div>
          )}

          {video && <Track name={fileName(video)} url={video} kind="video" />}
        </section>
      </div>
    </>
  )
}
