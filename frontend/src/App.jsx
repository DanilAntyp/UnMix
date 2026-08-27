import { useState } from 'react'
import { DownloadView } from './views/Download'
import { ExtractView } from './views/Extract'
import { KaraokeView } from './views/Karaoke'
import { ConvertView } from './views/Convert'
import { IconDownload, IconWave, IconMic, IconScissors } from './ui'

const NAV = [
  { id: 'download', label: 'Download', icon: <IconDownload /> },
  { id: 'extract', label: 'Extract', icon: <IconWave /> },
  { id: 'karaoke', label: 'Karaoke', icon: <IconMic /> },
  { id: 'convert', label: 'Convert & Trim', icon: <IconScissors /> },
]

const HERO = {
  download: ['Grab a song', 'Paste a YouTube link — take the audio or the video with you.'],
  extract: ["Let's take it apart", 'AI splits any song into vocals, drums, bass and more.'],
  karaoke: ["Let's make karaoke", 'Mute the vocals — the lyrics appear in sync, word by word.'],
  convert: ['Reshape your audio', 'Convert between formats or cut out the part you need.'],
}

export default function App() {
  // deep link support: /karaoke?file=/downloads/... opens Karaoke preloaded
  const params = new URLSearchParams(location.search)
  const deepFile = location.pathname === '/karaoke' ? params.get('file') : null

  const [view, setView] = useState(deepFile ? 'karaoke' : (location.hash.slice(1) || 'download'))
  const [extractHandoff, setExtractHandoff] = useState(null)
  const [karaokeHandoff, setKaraokeHandoff] = useState(deepFile ? { file: deepFile } : null)

  function go(id) {
    setView(id)
    history.replaceState(null, '', '/#' + id)
  }

  const [title, sub] = HERO[view]

  return (
    <div className="layout">
      <div className="blobs">
        <div className="blob blob-1" />
        <div className="blob blob-2" />
        <div className="blob blob-3" />
      </div>
      <aside className="sidebar">
        <div className="logo">Un<em>Mix</em></div>
        {NAV.map(n => (
          <button key={n.id} className={'side-item' + (view === n.id ? ' active' : '')}
            onClick={() => go(n.id)}>
            {n.icon}{n.label}
          </button>
        ))}
        <div className="side-foot">
          runs locally on your machine<br />
          <a href="https://github.com/DanilAntyp/UnMix" target="_blank" rel="noreferrer">GitHub ↗</a>
        </div>
      </aside>

      <main className="main">
        <div className="view">
          <div className="hero">
            <h1>{title}</h1>
            <p>{sub}</p>
          </div>
          {/* all views stay mounted so running jobs keep polling across tabs */}
          <div style={{ display: view === 'download' ? 'block' : 'none' }}>
            <DownloadView
              onExtract={(file, title) => { setExtractHandoff({ file, title }); go('extract') }}
              onKaraoke={file => { setKaraokeHandoff({ file }); go('karaoke') }}
            />
          </div>
          <div style={{ display: view === 'extract' ? 'block' : 'none' }}>
            <ExtractView handoff={extractHandoff} />
          </div>
          <div style={{ display: view === 'karaoke' ? 'block' : 'none' }}>
            <KaraokeView handoff={karaokeHandoff} />
          </div>
          <div style={{ display: view === 'convert' ? 'block' : 'none' }}>
            <ConvertView />
          </div>
        </div>
      </main>
    </div>
  )
}
