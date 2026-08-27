import { useState } from 'react'
import { postJSON, pollProgress, fmtMB } from '../api'
import { Status, MediaCard, PanelHead, IconDownload } from '../ui'
import { Waveform } from '../Waveform'
import { ConvertControls } from '../ConvertControls'
import { LiquidMetalButton } from '../LiquidMetalButton'

export function DownloadView({ onExtract, onKaraoke }) {
  const [url, setUrl] = useState('')
  const [info, setInfo] = useState(null)
  const [status, setStatus] = useState(null) // {busy, text, pct, error}
  const [result, setResult] = useState(null) // {title, file, video}
  const [busy, setBusy] = useState(false)
  const [sel, setSel] = useState(null)       // waveform trim selection

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
      <div style={{ marginTop: 16, display: 'flex', justifyContent: 'center' }}>
        <LiquidMetalButton label="Get formats" width={170} onClick={getFormats} disabled={busy} />
      </div>
      <Status {...(status || {})} />

      {info && (
        <div className="fmt">
          <div className="fmt-head">
            {info.thumbnail && <img src={info.thumbnail} alt="" />}
            <div>
              <div className="fmt-name">{info.title}</div>
              <div className="fmt-meta">
                {info.channel}{info.channel && info.duration ? ' · ' : ''}
                {info.duration ? `${Math.floor(info.duration / 60)}:${String(Math.floor(info.duration % 60)).padStart(2, '0')}` : ''}
              </div>
            </div>
          </div>
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

      {result && result.video && (
        <div className="results">
          <MediaCard title={result.title} url={result.file} video />
        </div>
      )}
      {result && !result.video && (
        <div className="results">
          <div className="media-card glass-soft">
            <div className="media-row">
              <span className="media-name">{result.title}</span>
              <span className="media-actions">
                <button className="chip" onClick={() => onExtract(result.file, result.title)}>
                  Extract sound →
                </button>
                <button className="chip grad" onClick={() => onKaraoke(result.file)}>
                  Make karaoke →
                </button>
                <a className="chip" href={encodeURI(result.file)}
                  download={decodeURIComponent(result.file.split('/').pop())}>Download</a>
              </span>
            </div>
            <Waveform src={encodeURI(result.file)} selectable onSelect={setSel} />
            <ConvertControls serverFile={result.file} sel={sel} />
          </div>
        </div>
      )}
    </div>
  )
}
