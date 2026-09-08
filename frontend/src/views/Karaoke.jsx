import { useEffect, useRef, useState } from 'react'
import { postForm, postJSON } from '../api'
import { DropZone, Status, MediaCard, PanelHead, IconMic } from '../ui'
import { LiquidMetalButton } from '../LiquidMetalButton'

const STEM_OPTS = [
  { key: 'vocals', label: 'Vocals' },
  { key: 'drums', label: 'Drums' },
  { key: 'bass', label: 'Bass' },
  { key: 'other', label: 'Everything else' },
]

export function KaraokeView({ handoff }) {
  const [picked, setPicked] = useState(null)   // {file} or {server, title}
  const [remove, setRemove] = useState({ vocals: true })
  const [status, setStatus] = useState(null)
  const [video, setVideo] = useState(null)
  const [busy, setBusy] = useState(false)
  const pollRef = useRef(null)

  useEffect(() => {
    if (handoff) {
      setPicked({ server: handoff.file, title: decodeURIComponent(handoff.file.split('/').pop()) })
      setVideo(null); setStatus(null)
    }
  }, [handoff])

  useEffect(() => () => clearTimeout(pollRef.current), [])

  function pickLocal(f) {
    setPicked({ file: f, title: f.name })
    setVideo(null); setStatus(null)
  }

  async function start() {
    const stems = Object.keys(remove).filter(k => remove[k])
    if (!picked || busy) return
    if (!stems.length) { setStatus({ text: 'Pick at least one thing to remove.', error: true }); return }
    setBusy(true); setVideo(null)
    setStatus({ busy: true, text: 'Starting…' })
    try {
      let data
      if (picked.file) {
        const fd = new FormData()
        fd.append('audio', picked.file)
        fd.append('remove', stems.join(','))
        data = await postForm('/karaoke/start', fd)
      } else {
        data = await postJSON('/karaoke/start', { server_file: picked.server, remove: stems.join(',') })
      }
      poll(data.job)
    } catch (e) {
      setStatus({ text: 'Error: ' + e.message, error: true })
      setBusy(false)
    }
  }

  async function poll(id) {
    try {
      const j = await (await fetch('/karaoke/status/' + id)).json()
      if (j.error && !j.done) throw new Error(j.error)
      if (!j.done) {
        setStatus({ busy: true, text: j.stage, pct: j.pct })
        pollRef.current = setTimeout(() => poll(id), 1500)
        return
      }
      if (j.error) throw new Error(j.error)
      setStatus({ text: j.stage })
      setVideo(j.video)
      setBusy(false)
    } catch (e) {
      setStatus({ text: 'Error: ' + e.message, error: true })
      setBusy(false)
    }
  }

  return (
    <div className="glass metal-scope">
      <PanelHead
        tile="tile-rose" icon={<IconMic />}
        title="Your voice. Center stage." sub="Create an instrumental and a video with word-synced lyrics."
      />
      <DropZone onFile={pickLocal} icon={<IconMic width={30} height={30} color="#9a9aa8" />}
        hint="mp3, wav, flac, m4a …" accept="audio/*,.mp3,.wav,.flac,.m4a,.ogg,.aac" />
      {picked && <div className="picked">♪ {picked.title}</div>}

      <div className="pills">
        {STEM_OPTS.map(({ key, label }) => (
          <label key={key} className={'pill' + (remove[key] ? ' on' : '')}>
            <input type="checkbox" checked={!!remove[key]}
              onChange={e => setRemove({ ...remove, [key]: e.target.checked })} />
            {remove[key] ? '✓ ' : ''}Mute {label.toLowerCase()}
          </label>
        ))}
      </div>

      {picked && (
        <div style={{ marginTop: 20, display: 'flex', justifyContent: 'center' }}>
          <LiquidMetalButton label="Create karaoke video" width={220} onClick={start} disabled={busy} />
        </div>
      )}
      <Status {...(status || {})} />

      {video && (
        <div className="results">
          <MediaCard title="Karaoke video" url={video} video />
        </div>
      )}
    </div>
  )
}
