import { useState } from 'react'
import Backdrop from './components/Backdrop'
import YouTubePanel from './panels/YouTubePanel'
import SeparatePanel from './panels/SeparatePanel'
import ConvertPanel from './panels/ConvertPanel'

export default function HomePage() {
  // Bumped every time the YouTube panel hands a downloaded file to the
  // separator, so the same file can be sent over twice.
  const [extract, setExtract] = useState(null)

  return (
    <>
      <Backdrop />
      <div className="shell">
        <header className="masthead">
          <span className="eyebrow"><span className="dot" /> Runs entirely on your machine</span>
          <h1 className="wordmark">UnMix</h1>
          <p className="tagline">
            Pull music off YouTube, tear any song into <b>stems</b>, cut it,
            convert it — and turn it into a <b>karaoke video</b>.
          </p>
          <a
            className="cta"
            href="/karaoke"
            onClick={e => {
              e.preventDefault()
              window.open('/karaoke', 'karaoke', 'width=780,height=980')
            }}
          >
            <span className="inner">
              &#127908; Open Karaoke Mode <span className="arrow">&#8599;</span>
            </span>
          </a>
        </header>

        <main className="grid">
          <YouTubePanel
            onExtract={(file, title) => setExtract({ file, title, id: Date.now() })}
          />
          <SeparatePanel request={extract} />
          <ConvertPanel />
        </main>

        <footer className="foot">
          <span>Demucs for the stems</span>
          <span className="sep">·</span>
          <span>Whisper for the words</span>
          <span className="sep">·</span>
          <span>nothing leaves your computer</span>
        </footer>
      </div>
    </>
  )
}
