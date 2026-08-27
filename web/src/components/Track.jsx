import { fileName } from '../api'

/** A finished file: name, actions, and an inline player. */
export default function Track({ name, url, kind = 'audio', dot, actions }) {
  const src = encodeURI(url)
  return (
    <div className="track">
      <div className="top">
        {dot && <span className="stem-dot" style={{ background: dot, color: dot }} />}
        <span className="nm" title={name}>{name}</span>
        <span className="acts">
          {actions}
          <a className="btn-ghost" href={src} download={fileName(url)}>&#8681; Save</a>
        </span>
      </div>
      {kind === 'video'
        ? <video controls src={src} />
        : <audio controls src={src} />}
    </div>
  )
}
