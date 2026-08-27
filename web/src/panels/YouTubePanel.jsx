import { useState } from 'react'
import Panel from '../components/Panel'
import Icon from '../components/Icon'
import Status from '../components/Status'
import Track from '../components/Track'
import { postJSON, pollProgress, fmtSize } from '../api'

const KARAOKE_WINDOW = 'width=780,height=980'

export default function YouTubePanel({ onExtract }) {
  const [url, setUrl] = useState('')
  const [info, setInfo] = useState(null)      // { title, audio[], video[] }
  const [status, setStatus] = useState(null)
  const [busy, setBusy] = useState(false)     // a fetch/download is in flight
  const [result, setResult] = useState(null)  // { title, file, kind }

  async function getFormats() {
    const link = url.trim()
    if (!link) { setStatus({ kind: 'error', text: 'Paste a YouTube link first.' }); return }
    setBusy(true); setInfo(null); setResult(null)
    setStatus({ kind: 'busy', text: 'Reading video info…' })
    try {
      const data = await postJSON('/yt/info', { url: link })
      setInfo(data)
      setStatus(null)
    } catch (err) {
      setStatus({ kind: 'error', text: err.message })
    } finally {
      setBusy(false)
    }
  }

  async function download(spec, toKaraoke) {
    // The popup has to be opened inside the click handler or the browser
    // blocks it; it is pointed at the song once the download finishes.
    const kw = toKaraoke ? window.open('', 'karaoke', KARAOKE_WINDOW) : null
    if (kw) {
      kw.document.write(
        '<body style="background:#07070c;color:#a8a4c4;font-family:system-ui;' +
        'display:flex;align-items:center;justify-content:center;height:95vh">' +
        'Downloading the song…</body>')
    }
    setBusy(true); setResult(null)
    setStatus({ kind: 'busy', text: 'Downloading…', pct: 0 })
    const pid = 'yt' + Date.now()
    const stop = pollProgress('/progress/' + pid, pct =>
      setStatus({ kind: 'busy', text: `Downloading… ${Math.round(pct)}%`, pct }))
    try {
      const data = await postJSON('/yt', { url: url.trim(), pid, ...spec })
      if (kw) kw.location = '/karaoke?file=' + encodeURIComponent(data.file)
      setResult({ ...data, kind: spec.kind })
      setStatus({ kind: 'done', text: 'Saved to downloads/' })
    } catch (err) {
      if (kw) kw.close()
      setStatus({ kind: 'error', text: err.message })
    } finally {
      stop()
      setBusy(false)
    }
  }

  return (
    <Panel
      mark={<Icon name="download" />}
      title="YouTube Downloader"
      desc="Paste a link, pick a quality — audio or video, straight to disk."
      accent="#ff2e9a" accent2="#ff7a1a"
    >
      <div className="field">
        <span className="label">Video link</span>
        <input
          type="url"
          value={url}
          placeholder="https://www.youtube.com/watch?v=…"
          onChange={e => setUrl(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') getFormats() }}
        />
      </div>
      <button className="btn block" style={{ marginTop: 12 }} onClick={getFormats} disabled={busy}>
        Get formats
      </button>

      <Status state={status} />

      {info && (
        <div className="formats">
          <div className="vid-title" title={info.title}>{info.title}</div>

          <div className="group">&#9835; Audio</div>
          {info.audio.map(a => (
            <FormatRow
              key={'a' + a.abr}
              label={a.label}
              size={a.size}
              busy={busy}
              onDownload={() => download({ kind: 'audio', abr: a.abr })}
              onKaraoke={() => download({ kind: 'audio', abr: a.abr }, true)}
            />
          ))}

          {info.video.length > 0 && <div className="group">&#9654; Video</div>}
          {info.video.map(v => (
            <FormatRow
              key={'v' + v.height}
              label={v.label}
              chip="mp4 · avc1"
              size={v.size}
              busy={busy}
              onDownload={() => download({ kind: 'video', height: v.height })}
            />
          ))}
        </div>
      )}

      {result && (
        <Track
          name={result.title}
          url={result.file}
          kind={result.kind}
          actions={result.kind === 'audio' && (
            <>
              <button
                className="btn-ghost"
                onClick={() => window.open(
                  '/karaoke?file=' + encodeURIComponent(result.file), 'karaoke', KARAOKE_WINDOW)}
              >&#127908; Karaoke</button>
              <button
                className="btn-ghost"
                onClick={() => onExtract(result.file, result.title)}
              >&#9889; Split</button>
            </>
          )}
        />
      )}
    </Panel>
  )
}

function FormatRow({ label, chip, size, busy, onDownload, onKaraoke }) {
  return (
    <div className="fmt-row">
      <span className="lbl">
        {label}
        {chip && <span className="chip">{chip}</span>}
      </span>
      <span className="size">{fmtSize(size)}</span>
      <span className="acts">
        <button className="btn-sm" onClick={onDownload} disabled={busy}>&#8681; Get</button>
        {onKaraoke && (
          <button className="btn-sm alt" onClick={onKaraoke} disabled={busy}>&#127908; Karaoke</button>
        )}
      </span>
    </div>
  )
}
