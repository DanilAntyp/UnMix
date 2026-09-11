import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowLeft, ArrowRight, ArrowUp, ChevronRight, Disc3, Download, FileVideo,
  Folder, FolderOpen, FolderPlus, LayoutGrid, List, Music4, Piano, Plus,
  Search, SlidersHorizontal, Sparkles, Trash2, Waves,
} from 'lucide-react'
import { postJSON, postForm } from '../api'
import { PanelHead, TrackFacts, IconNote } from '../ui'
import { Waveform } from '../Waveform'

const ALL = 'all'                       // smart view: every file, folders flattened
const SORTS = [['name', 'Name'], ['mtime', 'Date added'], ['size', 'Size'], ['kind', 'Kind']]
const ROOT_ICONS = {
  downloads: <Music4 />, stems: <Waves />, converted: <SlidersHorizontal />,
  mixes: <Disc3 />, karaoke: <FileVideo />, midi: <Piano />,
}

/* encodeURI leaves #, ? and % alone — fatal for real song filenames */
const mediaURL = p => p.split('/').map(encodeURIComponent).join('/')
const parentOf = p => p.slice(0, p.lastIndexOf('/'))
const isUnder = (path, dir) => path === dir || path.startsWith(dir + '/')

const fmtSize = b => (b > 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.round(b / 1024) + ' KB')
const fmtDate = ts => new Date(ts * 1000).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: '2-digit' })

function crumbsFor(path, roots) {
  if (path === ALL) return [{ label: 'All files', path: ALL }]
  const root = roots.find(r => isUnder(path, r.path))
  if (!root) return []
  const out = [{ label: root.label, path: root.path }]
  let acc = root.path
  for (const seg of path.slice(root.path.length).split('/').filter(Boolean)) {
    acc += '/' + seg
    out.push({ label: seg, path: acc })
  }
  return out
}

/* ---------- inline rename field, Finder-style (extension left out of the selection) ---------- */
function NameField({ value, isFile, onCommit, onCancel }) {
  const [draft, setDraft] = useState(value)
  const ref = useRef()
  const closed = useRef(false)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.focus()
    const dot = value.lastIndexOf('.')
    el.setSelectionRange(0, isFile && dot > 0 ? dot : value.length)
  }, [])
  const finish = fn => { if (!closed.current) { closed.current = true; fn() } }
  return (
    <input
      ref={ref} className="fin-rename" value={draft} aria-label="New name"
      onChange={e => setDraft(e.target.value)}
      onClick={e => e.stopPropagation()}
      onDoubleClick={e => e.stopPropagation()}
      onBlur={() => finish(() => onCommit(draft))}
      onKeyDown={e => {
        e.stopPropagation()
        if (e.key === 'Enter') { e.preventDefault(); finish(() => onCommit(draft)) }
        if (e.key === 'Escape') { e.preventDefault(); finish(onCancel) }
      }}
    />
  )
}

/* ---------- a row in the sidebar: disclosure twist beside a navigation button ---------- */
function SideRow({ path, label, icon, count, indent, expandable, expanded, cwd, toggle, onOpen, drop }) {
  return (
    <div className={'fin-src-row' + (drop.over === path ? ' drop' : '')} {...drop.handlers(path)}>
      <button
        className={'fin-twist' + (expanded ? ' open' : '') + (expandable ? '' : ' hidden')}
        style={{ marginLeft: indent }} tabIndex={expandable ? 0 : -1}
        aria-label={`${expanded ? 'Collapse' : 'Expand'} ${label}`} aria-expanded={expandable ? expanded : undefined}
        onClick={() => toggle(path)}
      ><ChevronRight size={12} /></button>
      <button className={'fin-src' + (cwd === path ? ' on' : '')} onClick={() => onOpen(path)}>
        {icon}<span>{label}</span>{count != null && <em>{count}</em>}
      </button>
    </div>
  )
}

/* ---------- sidebar disclosure tree ---------- */
function TreeBranch({ dir, childrenOf, depth, cwd, open, toggle, onOpen, drop }) {
  const kids = childrenOf[dir] || []
  if (!kids.length) return null
  return (
    <ul className="fin-tree">
      {kids.map(f => (
        <li key={f.path}>
          <SideRow
            path={f.path} label={f.name} indent={depth * 13} cwd={cwd}
            icon={open.has(f.path) ? <FolderOpen size={14} /> : <Folder size={14} />}
            expandable={(childrenOf[f.path] || []).length > 0} expanded={open.has(f.path)}
            toggle={toggle} onOpen={onOpen} drop={drop}
          />
          {open.has(f.path) && (
            <TreeBranch dir={f.path} childrenOf={childrenOf} depth={depth + 1}
              cwd={cwd} open={open} toggle={toggle} onOpen={onOpen} drop={drop} />
          )}
        </li>
      ))}
    </ul>
  )
}

export function LibraryView({ onDJ }) {
  const [tree, setTree] = useState({ roots: [], folders: [], items: [] })
  const [nav, setNav] = useState({ stack: ['/downloads'], idx: 0 })
  const [sel, setSel] = useState(() => new Set())
  const [q, setQ] = useState('')
  const [sort, setSort] = useState({ key: 'name', dir: 1 })
  const [layout, setLayout] = useState(() => {
    try { return localStorage.getItem('unmix-lib-layout') || 'list' } catch { return 'list' }
  })
  const [expanded, setExpanded] = useState(() => new Set())
  const [renaming, setRenaming] = useState(null)   // path being renamed
  const [menu, setMenu] = useState(null)           // { x, y, entry }
  const [ask, setAsk] = useState(null)             // confirm dialog
  const [over, setOver] = useState(null)           // drop target path
  const [note, setNote] = useState({ text: '', bad: false })
  const [busy, setBusy] = useState(false)
  const [recog, setRecog] = useState({})
  const dragging = useRef([])
  const fileInput = useRef()
  const cwd = nav.stack[nav.idx]

  const load = useCallback(() => fetch('/library')
    .then(r => r.json())
    .then(d => setTree({ roots: d.roots || [], folders: d.folders || [], items: d.items || [] }))
    .catch(() => setNote({ text: 'could not reach the library', bad: true })), [])
  useEffect(() => { load() }, [load])

  /* ----- derived structure ----- */
  const roots = tree.roots
  const isRoot = useCallback(p => p === ALL || roots.some(r => r.path === p), [roots])

  const childFolders = useMemo(() => {
    const m = {}
    for (const f of tree.folders) (m[f.dir] ||= []).push(f)
    for (const list of Object.values(m)) list.sort((a, b) => a.name.localeCompare(b.name))
    return m
  }, [tree.folders])

  const counts = useMemo(() => {           // direct children, for the "N items" column
    const m = {}
    for (const f of tree.folders) m[f.dir] = (m[f.dir] || 0) + 1
    for (const it of tree.items) m[it.dir] = (m[it.dir] || 0) + 1
    return m
  }, [tree])

  const totals = useMemo(() => {           // everything filed below, for the sources list
    const m = {}
    for (const it of tree.items) {
      for (let d = it.dir; d.includes('/'); d = parentOf(d)) {
        m[d] = (m[d] || 0) + 1
        if (roots.some(r => r.path === d)) break
      }
    }
    return m
  }, [tree.items, roots])

  const toggle = useCallback(path => setExpanded(s => {
    const n = new Set(s)
    n.has(path) ? n.delete(path) : n.add(path)
    return n
  }), [])

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase()
    const asFolder = f => ({ ...f, type: 'folder', size: 0, count: counts[f.path] || 0 })
    const asFile = it => ({ ...it, type: 'file', path: it.file })
    let folders = [], files = []
    if (needle) {
      // searching descends: everything filed below here, however deep
      const scope = cwd === ALL ? null : cwd
      folders = tree.folders.filter(f => (!scope || isUnder(f.path, scope)) && f.path !== scope &&
        f.name.toLowerCase().includes(needle)).map(asFolder)
      files = tree.items.filter(it => (!scope || isUnder(it.file, scope)) &&
        it.name.toLowerCase().includes(needle)).map(asFile)
    } else if (cwd === ALL) {
      files = tree.items.map(asFile)
    } else {
      folders = (childFolders[cwd] || []).map(asFolder)
      files = tree.items.filter(it => it.dir === cwd).map(asFile)
    }
    const cmp = (a, b) => {
      const k = sort.key
      const v = k === 'mtime' ? a.mtime - b.mtime
        : k === 'size' ? (a.size || 0) - (b.size || 0)
          : k === 'kind' ? (a.kind || '').localeCompare(b.kind || '')
            : 0
      return ((v || a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }))) * sort.dir
    }
    return [...folders.sort(cmp), ...files.sort(cmp)]   // folders lead, as in Finder
  }, [tree, cwd, q, sort, childFolders, counts])

  const byPath = useMemo(() => Object.fromEntries(rows.map(r => [r.path, r])), [rows])
  const selected = useMemo(() => [...sel].filter(p => byPath[p]), [sel, byPath])
  const onlyFile = selected.length === 1 && byPath[selected[0]]?.type === 'file' ? byPath[selected[0]] : null
  const uploadDir = cwd === ALL ? '/downloads' : cwd
  const crumbs = crumbsFor(cwd, roots)

  /* ----- navigation ----- */
  const go = useCallback(path => setNav(n => (path === n.stack[n.idx] ? n
    : { stack: [...n.stack.slice(0, n.idx + 1), path], idx: n.idx + 1 })), [])
  useEffect(() => { setSel(new Set()); setQ(''); setRenaming(null); setMenu(null) }, [cwd])

  function openEntry(entry) {
    if (entry.type === 'folder') {
      setExpanded(s => new Set(s).add(parentOf(entry.path)))
      go(entry.path)
    } else setSel(new Set([entry.path]))
  }

  /* ----- mutations ----- */
  async function run(label, fn) {
    setBusy(true); setNote({ text: label, bad: false })
    try {
      const out = await fn()
      const errs = out?.errors?.length ? ' — ' + out.errors.join('; ') : ''
      setNote({ text: (out?.done || 'done') + errs, bad: !!errs })
      return out
    } catch (e) {
      setNote({ text: e.message || 'that did not work', bad: true })
    } finally {
      setBusy(false)
      await load()
    }
  }

  const move = (paths, dir) => {
    const clean = paths.filter(p => p !== dir && parentOf(p) !== dir && !isUnder(dir, p))
    if (!clean.length) return
    return run('moving…', async () => {
      const d = await postJSON('/library/move', { dir, files: clean })
      setSel(new Set(d.moved || []))
      return { ...d, done: `moved ${d.moved.length} to ${dir.split('/').pop()}` }
    })
  }

  const upload = (files, dir) => {
    const fd = new FormData()
    fd.append('dir', dir)
    for (const f of files) fd.append('files', f)
    return run(`adding ${files.length} file${files.length > 1 ? 's' : ''}…`, async () => {
      const d = await postForm('/library/upload', fd)
      setSel(new Set(d.added || []))
      return { ...d, done: `added ${d.added.length} to the library` }
    })
  }

  async function newFolder() {
    const taken = new Set((childFolders[uploadDir] || []).map(f => f.name))
    let name = 'New folder'
    for (let n = 2; taken.has(name); n++) name = `New folder ${n}`
    const d = await run('creating folder…', async () => {
      const r = await postJSON('/library/folder', { dir: uploadDir, name })
      return { ...r, done: `“${name}” created` }
    })
    if (d?.path) {
      if (cwd === ALL) go(uploadDir)
      setSel(new Set([d.path]))
      setRenaming(d.path)                       // land straight in rename, like Finder
    }
  }

  const rename = (entry, name) => {
    setRenaming(null)
    if (!name.trim() || name === entry.name) return
    return run('renaming…', async () => {
      const d = await postJSON('/library/rename', { path: entry.path, name })
      setSel(new Set([d.path]))
      if (entry.type === 'folder' && isUnder(cwd, entry.path)) go(d.path + cwd.slice(entry.path.length))
      return { ...d, done: `renamed to ${d.name}` }
    })
  }

  function remove(paths) {
    const entries = paths.map(p => byPath[p]).filter(Boolean)
    if (!entries.length) return
    const folders = entries.filter(e => e.type === 'folder')
    const inside = folders.reduce((n, f) => n + (counts[f.path] || 0), 0)
    setAsk({
      title: entries.length === 1 ? `Delete “${entries[0].name}”?` : `Delete ${entries.length} items?`,
      body: inside
        ? `${folders.length === 1 ? 'That folder holds' : 'Those folders hold'} ${inside} item${inside > 1 ? 's' : ''}, and they go too. This cannot be undone.`
        : 'The files are removed from disk. This cannot be undone.',
      ok: 'Delete',
      run: () => run('deleting…', async () => {
        const d = await postJSON('/library/delete', { files: paths })
        setSel(new Set())
        return { ...d, done: `deleted ${d.removed} item${d.removed === 1 ? '' : 's'}` }
      }),
    })
  }

  async function recognize(entry) {
    setRecog(r => ({ ...r, [entry.path]: { busy: true } }))
    try {
      const found = await postJSON('/recognize', { file: entry.path })
      setRecog(r => ({ ...r, [entry.path]: found }))
    } catch (e) {
      setRecog(r => ({ ...r, [entry.path]: { error: e.message } }))
    }
  }

  /* ----- selection ----- */
  function pick(e, entry) {
    const meta = e.metaKey || e.ctrlKey
    if (e.shiftKey && sel.size) {
      const order = rows.map(r => r.path)
      const anchor = order.findIndex(p => sel.has(p))
      const here = order.indexOf(entry.path)
      const [a, b] = anchor < here ? [anchor, here] : [here, anchor]
      setSel(new Set(order.slice(a, b + 1)))
    } else if (meta) {
      setSel(s => { const n = new Set(s); n.has(entry.path) ? n.delete(entry.path) : n.add(entry.path); return n })
    } else setSel(new Set([entry.path]))
  }

  /* ----- drag and drop ----- */
  const dropOn = useCallback(dir => ({
    onDragOver: e => {
      if (!dir || dir === ALL) return
      e.preventDefault(); e.stopPropagation()
      e.dataTransfer.dropEffect = e.dataTransfer.types.includes('Files') ? 'copy' : 'move'
      setOver(dir)
    },
    onDragLeave: e => { e.stopPropagation(); setOver(o => (o === dir ? null : o)) },
    onDrop: e => {
      if (!dir || dir === ALL) return
      e.preventDefault(); e.stopPropagation()
      setOver(null)
      const dropped = [...(e.dataTransfer.files || [])]
      const paths = dragging.current
      dragging.current = []
      if (dropped.length) upload(dropped, dir)
      else if (paths.length) move(paths, dir)
    },
  }), [move, upload])
  const drop = useMemo(() => ({ over, handlers: dropOn }), [over, dropOn])

  function startDrag(e, entry) {
    const paths = sel.has(entry.path) ? [...sel] : [entry.path]
    if (!sel.has(entry.path)) setSel(new Set([entry.path]))
    dragging.current = paths
    e.dataTransfer.effectAllowed = 'move'
    e.dataTransfer.setData('text/plain', paths.join('\n'))
  }

  /* ----- keyboard ----- */
  useEffect(() => {
    function onKey(e) {
      if (menu) { setMenu(null); if (e.key === 'Escape') return }
      const tag = e.target.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || renaming) return
      if (e.key === 'Escape') { setSel(new Set()); setAsk(null) }
      else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'a') {
        e.preventDefault(); setSel(new Set(rows.map(r => r.path)))
      } else if ((e.key === 'Backspace' || e.key === 'Delete') && selected.length) {
        e.preventDefault(); remove(selected)
      } else if (e.key === 'F2' && selected.length === 1) {
        e.preventDefault(); setRenaming(selected[0])
      } else if (e.key === 'Enter' && selected.length === 1) {
        e.preventDefault(); openEntry(byPath[selected[0]])
      } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        if (!rows.length) return
        e.preventDefault()
        const order = rows.map(r => r.path)
        const at = order.indexOf(selected[selected.length - 1])
        const next = e.key === 'ArrowDown'
          ? Math.min(order.length - 1, at + 1) : Math.max(0, at < 0 ? 0 : at - 1)
        setSel(new Set([order[next]]))
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [rows, selected, byPath, renaming, menu])

  useEffect(() => {
    if (!menu) return
    const close = () => setMenu(null)
    window.addEventListener('click', close)
    window.addEventListener('scroll', close, true)
    return () => { window.removeEventListener('click', close); window.removeEventListener('scroll', close, true) }
  }, [menu])

  useEffect(() => {
    try { localStorage.setItem('unmix-lib-layout', layout) } catch {}
  }, [layout])

  /* ----- context menu items ----- */
  const menuItems = entry => {
    const many = sel.size > 1 && sel.has(entry.path)
    const targets = many ? [...sel] : [entry.path]
    const out = [
      { label: entry.type === 'folder' ? 'Open' : 'Preview', run: () => openEntry(entry) },
    ]
    if (!many) out.push({ label: 'Rename', run: () => { setSel(new Set([entry.path])); setRenaming(entry.path) } })
    if (entry.type === 'file') {
      if (!many) out.push({ label: 'Download', href: mediaURL(entry.path), name: entry.name })
      if (!entry.video && !entry.midi && !many) {
        out.push({ label: 'Find a mix partner', run: () => onDJ?.(entry.path) })
        out.push({ label: 'Recognize song', run: () => recognize(entry) })
      }
    }
    const parent = parentOf(entry.path)
    if (!isRoot(parent)) out.push({ label: 'Move up one folder', run: () => move(targets, parentOf(parent)) })
    out.push({ label: many ? `Delete ${sel.size} items` : 'Delete', danger: true, run: () => remove(targets) })
    return out
  }

  /* ----- render ----- */
  return (
    <div className="glass metal-scope fin">
      <PanelHead
        tile="tile-mint" icon={<IconNote />}
        title="The collection" sub="Your sources, experiments, and finished work. File it the way you think about it."
      />

      <div className="fin-bar">
        <div className="fin-steps">
          <button className="fin-icon" onClick={() => setNav(n => ({ ...n, idx: Math.max(0, n.idx - 1) }))}
            disabled={nav.idx === 0} aria-label="Back"><ArrowLeft size={15} /></button>
          <button className="fin-icon" onClick={() => setNav(n => ({ ...n, idx: Math.min(n.stack.length - 1, n.idx + 1) }))}
            disabled={nav.idx >= nav.stack.length - 1} aria-label="Forward"><ArrowRight size={15} /></button>
          <button className="fin-icon" onClick={() => go(parentOf(cwd))}
            disabled={isRoot(cwd)} aria-label="Enclosing folder"><ArrowUp size={15} /></button>
        </div>
        <nav className="fin-crumbs" aria-label="Folder path">
          {crumbs.map((c, i) => (
            <span key={c.path}>
              {i > 0 && <ChevronRight size={12} className="fin-crumb-sep" />}
              <button className={'fin-crumb' + (i === crumbs.length - 1 ? ' on' : '') + (over === c.path ? ' drop' : '')}
                onClick={() => go(c.path)} {...drop.handlers(c.path === ALL ? null : c.path)}>{c.label}</button>
            </span>
          ))}
        </nav>
        <label className="fin-search">
          <Search size={14} />
          <input value={q} onChange={e => setQ(e.target.value)} aria-label="Search your collection"
            placeholder={cwd === ALL ? 'Search everything…' : `Search ${crumbs[crumbs.length - 1]?.label || ''}…`} />
          {q && <button className="fin-clear" onClick={() => setQ('')} aria-label="Clear search">×</button>}
        </label>
      </div>

      <div className="fin-actions">
        <button className="btn-primary" onClick={() => fileInput.current.click()} disabled={busy}>
          <Plus size={15} /> Add music
        </button>
        <button className="chip" onClick={newFolder} disabled={busy}>
          <FolderPlus size={14} /> New folder
        </button>
        <input ref={fileInput} type="file" hidden multiple
          accept="audio/*,video/*,.mp3,.wav,.flac,.m4a,.ogg,.aac,.mp4,.mid"
          onChange={e => { if (e.target.files.length) upload([...e.target.files], uploadDir); e.target.value = '' }} />
        {selected.length > 0 && (
          <>
            <span className="fin-sep" />
            {selected.length === 1 && (
              <button className="chip" onClick={() => setRenaming(selected[0])}>Rename</button>
            )}
            <button className="chip fin-danger" onClick={() => remove(selected)}>
              <Trash2 size={13} /> Delete
            </button>
            <span className="wave-hint">{selected.length} selected</span>
          </>
        )}
        <span className="fin-spacer" />
        <select className="fin-sort" value={sort.key} aria-label="Sort by"
          onChange={e => setSort(s => ({ ...s, key: e.target.value }))}>
          {SORTS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
        </select>
        <button className="fin-icon" onClick={() => setSort(s => ({ ...s, dir: -s.dir }))}
          aria-label={sort.dir > 0 ? 'Sort ascending' : 'Sort descending'}>{sort.dir > 0 ? '↑' : '↓'}</button>
        <button className="fin-icon" onClick={() => setLayout(l => (l === 'list' ? 'grid' : 'list'))}
          aria-label={layout === 'list' ? 'Switch to grid' : 'Switch to list'}>
          {layout === 'list' ? <LayoutGrid size={15} /> : <List size={15} />}
        </button>
      </div>

      {note.text && (
        <div className={'fin-note' + (note.bad ? ' bad' : '')} role="status" aria-live="polite">
          {busy && <span className="spinner" />}{note.text}
        </div>
      )}

      <div className="fin-body">
        <aside className="fin-side">
          <p className="fin-side-head">Sources</p>
          <div className="fin-src-row">
            <span className="fin-twist hidden" />
            <button className={'fin-src' + (cwd === ALL ? ' on' : '')} onClick={() => go(ALL)}>
              <Sparkles size={15} /><span>All files</span><em>{tree.items.length}</em>
            </button>
          </div>
          {roots.map(r => (
            <div key={r.path}>
              <SideRow
                path={r.path} label={r.label} icon={ROOT_ICONS[r.kind] || <Folder size={15} />}
                count={totals[r.path] || 0} indent={0} cwd={cwd}
                expandable={(childFolders[r.path] || []).length > 0} expanded={expanded.has(r.path)}
                toggle={toggle} onOpen={go} drop={drop}
              />
              {expanded.has(r.path) && (
                <TreeBranch dir={r.path} childrenOf={childFolders} depth={1}
                  cwd={cwd} open={expanded} onOpen={go} toggle={toggle} drop={drop} />
              )}
            </div>
          ))}
          <p className="fin-side-note">Drag songs onto a folder to file them. Top-level sources cannot be renamed or removed.</p>
        </aside>

        <div
          className={'fin-pane ' + layout + (over === cwd ? ' drop' : '')}
          onClick={e => { if (e.target === e.currentTarget) setSel(new Set()) }}
          onContextMenu={e => {
            if (e.target.closest('.fin-item')) return
            e.preventDefault()
            setMenu({ x: e.clientX, y: e.clientY, items: [
              { label: 'New folder', run: newFolder },
              { label: 'Add music…', run: () => fileInput.current.click() },
              { label: 'Select all', run: () => setSel(new Set(rows.map(r => r.path))) },
            ] })
          }}
          {...drop.handlers(cwd === ALL ? null : cwd)}
        >
          {layout === 'list' && rows.length > 0 && (
            <div className="fin-head">
              <span className="fin-c-name">Name</span>
              <span className="fin-c-kind">Kind</span>
              <span className="fin-c-size">Size</span>
              <span className="fin-c-date">Added</span>
            </div>
          )}

          {rows.map(entry => {
            const on = sel.has(entry.path)
            const isFolder = entry.type === 'folder'
            return (
              <div
                key={entry.path}
                className={'fin-item' + (on ? ' on' : '') + (over === entry.path ? ' drop' : '')}
                draggable={!renaming}
                onDragStart={e => startDrag(e, entry)}
                onDragEnd={() => { dragging.current = []; setOver(null) }}
                onClick={e => pick(e, entry)}
                onDoubleClick={() => openEntry(entry)}
                onContextMenu={e => {
                  e.preventDefault(); e.stopPropagation()
                  if (!sel.has(entry.path)) setSel(new Set([entry.path]))
                  setMenu({ x: e.clientX, y: e.clientY, items: menuItems(entry) })
                }}
                {...(isFolder ? drop.handlers(entry.path) : {})}
              >
                <span className={'fin-glyph' + (isFolder ? ' folder' : '')}>
                  {isFolder ? <Folder size={layout === 'grid' ? 30 : 17} />
                    : entry.video ? <FileVideo size={layout === 'grid' ? 28 : 16} />
                      : entry.midi ? <Piano size={layout === 'grid' ? 28 : 16} />
                        : <Music4 size={layout === 'grid' ? 28 : 16} />}
                </span>
                <span className="fin-c-name">
                  {renaming === entry.path
                    ? <NameField value={entry.name} isFile={!isFolder}
                        onCommit={name => rename(entry, name)} onCancel={() => setRenaming(null)} />
                    : <span className="fin-label" title={entry.name}>{entry.name}</span>}
                  {(q || cwd === ALL) && (
                    <button className="fin-where" onClick={e => { e.stopPropagation(); go(parentOf(entry.path)) }}>
                      {crumbsFor(parentOf(entry.path), roots).map(c => c.label).join(' / ')}
                    </button>
                  )}
                </span>
                <span className="fin-c-kind">{isFolder ? `${entry.count} item${entry.count === 1 ? '' : 's'}` : entry.kind}</span>
                <span className="fin-c-size">
                  {!isFolder ? fmtSize(entry.size)
                    : layout === 'grid' ? `${entry.count} item${entry.count === 1 ? '' : 's'}` : '—'}
                </span>
                <span className="fin-c-date">{fmtDate(entry.mtime)}</span>
              </div>
            )
          })}

          {!rows.length && (
            <div className="fin-empty">
              <Folder size={26} />
              <p>{q ? `Nothing here matches “${q}”.`
                : cwd === ALL ? 'Your library is empty. Add music to begin.'
                  : 'This folder is empty. Drop songs in, or use Add music.'}</p>
            </div>
          )}
        </div>
      </div>

      {onlyFile && (
        <div className="fin-detail">
          <div className="fin-detail-head">
            <strong title={onlyFile.name}>{onlyFile.name}</strong>
            {!onlyFile.video && !onlyFile.midi && <TrackFacts url={onlyFile.path} />}
          </div>
          {onlyFile.video ? <video controls src={mediaURL(onlyFile.path)} />
            : onlyFile.midi ? <div className="wave-hint">MIDI file — download it and drop it into a DAW</div>
              : <Waveform src={mediaURL(onlyFile.path)} height={56} accent="#e8e8e8" />}
          <div className="lib-actions">
            <a className="chip" href={mediaURL(onlyFile.path)} download={onlyFile.name}>
              <Download size={13} /> Download
            </a>
            {!onlyFile.video && !onlyFile.midi && (
              <>
                <button className="chip" onClick={() => onDJ?.(onlyFile.path)}>✨ Find a mix partner</button>
                <button className="chip" onClick={() => recognize(onlyFile)} disabled={recog[onlyFile.path]?.busy}>
                  {recog[onlyFile.path]?.busy ? 'listening…' : 'Recognize song'}
                </button>
              </>
            )}
            <button className="chip" onClick={() => setRenaming(onlyFile.path)}>Rename</button>
            <button className="chip fin-danger" onClick={() => remove([onlyFile.path])}>Delete</button>
          </div>
          {recog[onlyFile.path] && !recog[onlyFile.path].busy && (
            <div className="lib-recog">
              {recog[onlyFile.path].error && <span className="wave-hint bad">{recog[onlyFile.path].error}</span>}
              {recog[onlyFile.path].found === false && <span className="wave-hint">no match found</span>}
              {recog[onlyFile.path].found && (
                <>
                  <span>♪ {recog[onlyFile.path].artist} — {recog[onlyFile.path].title}{' '}
                    <span className="wave-hint">(match {Math.round(recog[onlyFile.path].score * 100)}%)</span></span>
                  <button className="chip tiny" onClick={async () => {
                    await rename(onlyFile, recog[onlyFile.path].suggested)
                    setRecog(r => ({ ...r, [onlyFile.path]: undefined }))
                  }}>Rename file to this</button>
                </>
              )}
            </div>
          )}
        </div>
      )}

      {menu && (
        <ul className="fin-menu" style={{ left: menu.x, top: menu.y }} role="menu"
          onClick={e => e.stopPropagation()}>
          {menu.items.map((m, i) => (
            <li key={i}>
              {m.href
                ? <a role="menuitem" href={m.href} download={m.name} onClick={() => setMenu(null)}>{m.label}</a>
                : <button role="menuitem" className={m.danger ? 'danger' : ''}
                    onClick={() => { setMenu(null); m.run() }}>{m.label}</button>}
            </li>
          ))}
        </ul>
      )}

      {ask && (
        <div className="fin-modal" role="dialog" aria-modal="true" aria-label={ask.title}
          onClick={e => { if (e.target === e.currentTarget) setAsk(null) }}>
          <div className="fin-modal-card">
            <h3>{ask.title}</h3>
            <p>{ask.body}</p>
            <div className="fin-modal-actions">
              <button className="chip" onClick={() => setAsk(null)}>Cancel</button>
              <button className="btn-primary danger" onClick={() => { const f = ask.run; setAsk(null); f() }}>{ask.ok}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
