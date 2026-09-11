import { useMemo, useState } from 'react'
import { ArrowLeft, ArrowUp, ChevronRight, Folder, Music4, Search, X } from 'lucide-react'
import { TrackFacts } from './ui'

/* Browsing the same shape as the Library, but the rows are chosen rather than
   moved: folders to walk, a search that descends, and ticks that survive it. */

const ROOT = '/downloads'
const parentOf = p => p.slice(0, p.lastIndexOf('/'))
const isUnder = (path, dir) => path === dir || path.startsWith(dir + '/')
const fmtSize = b => (b > 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.round(b / 1024) + ' KB')

const where = dir => (dir === ROOT ? 'Downloads' : 'Downloads / ' + dir.slice(ROOT.length + 1).split('/').join(' / '))

export function TrackPicker({ tracks, folders, picked, onToggle, onClear }) {
  const [cwd, setCwd] = useState(ROOT)
  const [q, setQ] = useState('')

  const childFolders = useMemo(() => {
    const m = {}
    for (const f of folders) (m[f.dir] ||= []).push(f)
    for (const list of Object.values(m)) list.sort((a, b) => a.name.localeCompare(b.name))
    return m
  }, [folders])

  const counts = useMemo(() => {
    const m = {}
    for (const t of tracks) for (let d = t.dir; isUnder(d, ROOT); d = parentOf(d)) m[d] = (m[d] || 0) + 1
    return m
  }, [tracks])

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase()
    const byName = (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' })
    if (needle) {
      // search reaches into every folder below this one
      return [...tracks.filter(t => isUnder(t.file, cwd) && t.name.toLowerCase().includes(needle))
        .sort(byName).map(t => ({ ...t, kind: 'track' }))]
    }
    return [
      ...(childFolders[cwd] || []).sort(byName).map(f => ({ ...f, kind: 'folder' })),
      ...tracks.filter(t => t.dir === cwd).sort(byName).map(t => ({ ...t, kind: 'track' })),
    ]
  }, [tracks, childFolders, cwd, q])

  const crumbs = useMemo(() => {
    const out = [{ label: 'Downloads', path: ROOT }]
    let acc = ROOT
    for (const seg of cwd.slice(ROOT.length).split('/').filter(Boolean)) {
      acc += '/' + seg
      out.push({ label: seg, path: acc })
    }
    return out
  }, [cwd])

  const chosen = tracks.filter(t => picked[t.file])
  const visible = rows.filter(r => r.kind === 'track')
  const allOn = visible.length > 0 && visible.every(t => picked[t.file])

  const go = path => { setCwd(path); setQ('') }

  return (
    <>
      <div className="fin-bar">
        <div className="fin-steps">
          <button className="fin-icon" onClick={() => go(parentOf(cwd))}
            disabled={cwd === ROOT} aria-label="Back"><ArrowLeft size={15} /></button>
          <button className="fin-icon" onClick={() => go(parentOf(cwd))}
            disabled={cwd === ROOT} aria-label="Enclosing folder"><ArrowUp size={15} /></button>
        </div>
        <nav className="fin-crumbs" aria-label="Folder path">
          {crumbs.map((c, i) => (
            <span key={c.path}>
              {i > 0 && <ChevronRight size={12} className="fin-crumb-sep" />}
              <button className={'fin-crumb' + (i === crumbs.length - 1 ? ' on' : '')}
                onClick={() => go(c.path)}>{c.label}</button>
            </span>
          ))}
        </nav>
        <label className="fin-search">
          <Search size={14} />
          <input value={q} onChange={e => setQ(e.target.value)} aria-label="Search your records"
            placeholder={cwd === ROOT ? 'Search your records…' : `Search ${crumbs[crumbs.length - 1].label}…`} />
          {q && <button className="fin-clear" onClick={() => setQ('')} aria-label="Clear search">×</button>}
        </label>
        {visible.length > 0 && (
          <button className="chip tiny" onClick={() => visible.forEach(t => onToggle(t.file, !allOn))}>
            {allOn ? 'Deselect' : 'Select'} these {visible.length}
          </button>
        )}
      </div>

      <div className="fin-pane pick-pane">
        {rows.map(r => (r.kind === 'folder' ? (
          <button key={r.path} className="fin-item pick-folder" onClick={() => go(r.path)}>
            <span className="fin-glyph folder"><Folder size={17} /></span>
            <span className="fin-c-name"><span className="fin-label">{r.name}</span></span>
            <span className="fin-c-size">{counts[r.path] || 0} track{counts[r.path] === 1 ? '' : 's'}</span>
            <ChevronRight size={14} className="pick-into" />
          </button>
        ) : (
          <label key={r.file} className={'fin-item pick-track' + (picked[r.file] ? ' on' : '')}>
            <input className="pick-box" type="checkbox" checked={!!picked[r.file]}
              onChange={e => onToggle(r.file, e.target.checked)} />
            <span className="fin-glyph"><Music4 size={16} /></span>
            <span className="fin-c-name">
              <span className="fin-label" title={r.name}>{r.name}</span>
              {q && r.dir !== cwd && <span className="fin-where">{where(r.dir)}</span>}
            </span>
            {picked[r.file] && <TrackFacts url={r.file} />}
            <span className="fin-c-size">{fmtSize(r.size)}</span>
          </label>
        )))}
        {!rows.length && (
          <div className="fin-empty">
            <Folder size={26} />
            <p>{q ? `Nothing here matches “${q}”.`
              : cwd === ROOT ? 'No records yet — import some first.' : 'This folder has no tracks.'}</p>
          </div>
        )}
      </div>

      {chosen.length > 0 && (
        <div className="pick-bar">
          <span className="wave-hint">In the set</span>
          {chosen.map(t => (
            <button key={t.file} className="pick-chip" onClick={() => onToggle(t.file, false)}
              title={`${where(t.dir)} — click to remove`}>
              <span>{t.name}</span><X size={12} />
            </button>
          ))}
          <button className="chip tiny" onClick={onClear}>Clear all</button>
        </div>
      )}
    </>
  )
}
