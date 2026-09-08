import { useEffect, useState } from 'react'
import { AudioLines, Disc3, Library, Sparkles, ArrowRight } from 'lucide-react'
import { DownloadView } from './views/Download'
import { ExtractView } from './views/Extract'
import { StudioView } from './views/Studio'
import { DJView } from './views/DJ'
import { SetView } from './views/Set'
import { LibraryView } from './views/Library'
import { KaraokeView } from './views/Karaoke'
import { MidiView } from './views/Midi'
import { ConvertView } from './views/Convert'
import { IconDownload, IconWave, IconMic, IconSliders, IconPiano } from './ui'

const NAV = [
  { id: 'download', label: 'Import', detail: 'Download & convert', icon: <IconDownload /> },
  { id: 'stems', label: 'Stems', detail: 'Separate & remix', icon: <IconWave /> },
  { id: 'dj', label: 'DJ mixer', detail: 'Transition editor', icon: <Disc3 /> },
  { id: 'set', label: 'Sets', detail: 'Playlist mixing', icon: <IconSliders /> },
  { id: 'karaoke', label: 'Karaoke', detail: 'Instrumentals & lyrics', icon: <IconMic /> },
  { id: 'midi', label: 'MIDI', detail: 'Note transcription', icon: <IconPiano /> },
  { id: 'library', label: 'Library', detail: 'Files & exports', icon: <Library /> },
]

// old page ids that were merged away → where they live now
const ALIAS = { extract: 'stems', studio: 'stems', convert: 'download' }
const resolve = id => ALIAS[id] || (NAV.some(item => item.id === id) ? id : 'download')

export default function App() {
  // deep link support: /karaoke?file=/downloads/... opens Karaoke preloaded
  const params = new URLSearchParams(location.search)
  const deepFile = location.pathname === '/karaoke' ? params.get('file') : null

  const [view, setView] = useState(deepFile ? 'karaoke' : resolve(location.hash.slice(1) || 'download'))
  const [extractHandoff, setExtractHandoff] = useState(null)
  const [studioHandoff, setStudioHandoff] = useState(null)
  const [karaokeHandoff, setKaraokeHandoff] = useState(deepFile ? { file: deepFile } : null)
  const [djHandoff, setDjHandoff] = useState(null)
  const [motion, setMotion] = useState(() => {
    try { const saved = localStorage.getItem('unmix-motion'); if (saved) return saved === 'on' } catch {}
    return !window.matchMedia('(prefers-reduced-motion: reduce)').matches
  })
  useEffect(() => {
    const onHash = () => setView(resolve(location.hash.slice(1)))
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  function toggleMotion() {
    setMotion(current => {
      try { localStorage.setItem('unmix-motion', current ? 'off' : 'on') } catch {}
      return !current
    })
  }

  function go(id) {
    setView(id)
    history.replaceState(null, '', '/#' + id)
  }

  return (
    <div className="layout" data-motion={motion ? 'on' : 'off'} data-view={view}>
      <header className="sidebar">
        <button className="brand" onClick={() => go('download')} aria-label="UnMix home">
          <span className="brand-mark"><AudioLines /></span><span className="logo">unmix<span className="brand-dot">.</span></span>
        </button>
        <nav aria-label="Studio tools">{NAV.map(n => (
          <button key={n.id} className={'side-item' + (view === n.id ? ' active' : '')}
            aria-current={view === n.id ? 'page' : undefined} onClick={() => go(n.id)}>
            {n.icon}<span>{n.label}</span>
          </button>
        ))}</nav>
        <button className="motion-toggle" onClick={toggleMotion} aria-pressed={motion} aria-label={motion ? 'Pause visual effects' : 'Enable visual effects'}><Sparkles size={17} /><span>Effects {motion ? 'on' : 'off'}</span></button>
      </header>

      <main className="main">
        <div className={'view' + (view === 'dj' ? ' view-wide' : '')}>
          <div className="workspace-heading"><h1>{NAV.find(n=>n.id===view)?.detail}</h1><span><i className="status-dot" /> Runs on your machine</span></div>
          {/* all views stay mounted so running jobs keep polling across tabs */}
          <div className="rack-row" style={{ display: view === 'download' ? undefined : 'none' }}>
            <DownloadView
              onExtract={(file, title) => { setExtractHandoff({ file, title }); go('stems') }}
              onStudio={file => { setStudioHandoff({ file }); go('stems') }}
              onKaraoke={file => { setKaraokeHandoff({ file }); go('karaoke') }}
            />
            <ConvertView />
          </div>
          <div className="rack-stack" style={{ display: view === 'stems' ? undefined : 'none' }}>
            <ExtractView handoff={extractHandoff} />
            <StudioView handoff={studioHandoff} />
          </div>
          <div style={{ display: view === 'dj' ? 'block' : 'none' }}>
            <DJView handoff={djHandoff} />
          </div>
          <div style={{ display: view === 'set' ? 'block' : 'none' }}>
            <SetView />
          </div>
          <div style={{ display: view === 'midi' ? 'block' : 'none' }}>
            <MidiView />
          </div>
          <div style={{ display: view === 'karaoke' ? 'block' : 'none' }}>
            <KaraokeView handoff={karaokeHandoff} />
          </div>
          {view === 'library' && <LibraryView onDJ={file => { setDjHandoff({ file }); go('dj') }} />}
          <footer className="workspace-footer"><span>unmix. <i>Made for making.</i></span><a href="https://github.com/DanilAntyp/UnMix" target="_blank" rel="noreferrer">Under the hood <ArrowRight size={15} /></a></footer>
        </div>
      </main>
    </div>
  )
}
