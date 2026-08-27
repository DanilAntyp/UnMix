import { useState } from 'react'
import { postJSON, pollProgress, fmtMB } from '../api'
import { Status, MediaCard, PanelHead, IconDownload } from '../ui'

export function DownloadView({ onExtract, onKaraoke }) {
  const [url, setUrl] = useState('')
  const [info, setInfo] = useState(null)
  const [status, setStatus] = useState(null) // {busy, text, pct, error}
  const [result, setResult] = useState(null) // {title, file, video}
  const [busy, setBusy] = useState(false)

  async function getFormats() {
    if (!url.trim()) { setStatus({ text: 'Paste a YouTube link first.', error: true }); return }
    setBusy(true); setInfo(null); setResult(null)
    setStatus({ busy: true, text: 'Reading video info…' })
    try {
      const data = await postJSON('/yt/info', { url: url.trim() })
      setInfo(data)
      setStatus(null)
    } catch (e) {
      setStatus({ text: 'Error: ' + e.message, error: true })
    } finally { setBusy(false) }
  }

  async function download(spec, thenKaraoke) {
    const pid = 'yt' + Date.now()
    setBusy(true); setResult(null)
    setStatus({ busy: true, text: 'Downloading…', pct: 0 })
    const stop = pollProgress('/progress/' + pid, pct =>
      setStatus({ busy: true, text: 'Downloading…', pct }))
    try {
      const data = await postJSON('/yt', { url: url.trim(), pid, ...spec })
      stop()
      setStatus({ text: 'Done!' })
      setResult({ title: data.title, file: data.file, video: spec.kind === 'video' })
      if (thenKaraoke) onKaraoke(data.file)
    } catch (e) {
      stop()
      setStatus({ text: 'Error: ' + e.message, error: true })
    } finally { setBusy(false) }
  }

  return (
    <div className="glass">
      <PanelHead
        tile="tile-violet" icon={<IconDownload />}
        title="YouTube Downloader" sub="Paste a link, pick a quality"
      />
      <input
        className="input" type="url" value={url}
        placeholder="https://www.youtube.com/watch?v=…"
        onChange={e => setUrl(e.target.value)}
        onKeyDown={e => e.key === 'Enter' && getFormats()}
      />
      <div style={{ marginTop: 14 }}>
        <button className="btn-primary wide" onClick={getFormats} disabled={busy}>
          Get formats
        </button>
      </div>
      <Status {...(status || {})} />

      {info && (
        <div className="fmt">
          <div className="fmt-title">{info.title}</div>
          <table>
            <tbody>
              <tr><th colSpan={3}>Audio</th></tr>
              {info.audio.map(a => (
                <tr key={a.abr}>
                  <td>{a.label}</td>
                  <td className="size">{fmtMB(a.size)}</td>
                  <td className="actions">
                    <button className="chip" disabled={busy}
                      onClick={() => download({ kind: 'audio', abr: a.abr })}>Download</button>
                    <button className="chip grad" disabled={busy}
                      onClick={() => download({ kind: 'audio', abr: a.abr }, true)}>Karaoke</button>
                  </td>
                </tr>
              ))}
              {info.video.length > 0 && <tr><th colSpan={3}>Video</th></tr>}
              {info.video.map(v => (
                <tr key={v.height}>
                  <td>{v.label}<span className="badge">mp4 | avc1</span></td>
                  <td className="size">{fmtMB(v.size)}</td>
                  <td className="actions">
                    <button className="chip" disabled={busy}
                      onClick={() => download({ kind: 'video', height: v.height })}>Download</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {result && (
        <div className="results">
          <MediaCard
            title={result.title} url={result.file} video={result.video}
            actions={!result.video && (
              <>
                <button className="chip" onClick={() => onExtract(result.file, result.title)}>
                  Extract sound →
                </button>
                <button className="chip grad" onClick={() => onKaraoke(result.file)}>
                  Make karaoke →
                </button>
              </>
            )}
          />
        </div>
      )}
    </div>
  )
}
