import { useRef, useState } from 'react'
import Icon from './Icon'

/** Click-or-drag file picker. Calls onFile(File) once a file lands. */
export default function DropZone({ onFile, accept, lead, hint, icon = <Icon name="note" size={22} /> }) {
  const input = useRef(null)
  const [hover, setHover] = useState(false)

  const stop = e => { e.preventDefault(); e.stopPropagation() }
  const take = file => { if (file) onFile(file) }

  return (
    <div
      className={'drop' + (hover ? ' hover' : '')}
      onClick={() => input.current.click()}
      onDragEnter={e => { stop(e); setHover(true) }}
      onDragOver={e => { stop(e); setHover(true) }}
      onDragLeave={e => { stop(e); setHover(false) }}
      onDrop={e => { stop(e); setHover(false); take(e.dataTransfer.files[0]) }}
    >
      <div className="disc" aria-hidden="true">{icon}</div>
      <div className="lead">{lead}</div>
      {hint && <div className="hint">{hint}</div>}
      <input
        ref={input}
        type="file"
        accept={accept}
        onChange={e => { take(e.target.files[0]); e.target.value = '' }}
      />
    </div>
  )
}
