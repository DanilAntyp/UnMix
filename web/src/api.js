/* Thin wrappers over the Flask API in app.py / karaoke.py. Every endpoint
   answers with JSON and puts its failure message in `error`. */

async function unwrap(res) {
  let data = {}
  try { data = await res.json() } catch { /* empty or non-JSON body */ }
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`)
  return data
}

export function postJSON(url, body) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(unwrap)
}

export function postForm(url, fields) {
  const fd = new FormData()
  for (const [k, v] of Object.entries(fields)) fd.append(k, v)
  return fetch(url, { method: 'POST', body: fd }).then(unwrap)
}

export function getJSON(url) {
  return fetch(url).then(unwrap)
}

/** Poll a /progress endpoint until the returned stop() is called. */
export function pollProgress(url, onPct, interval = 600) {
  const id = setInterval(async () => {
    try {
      const p = await (await fetch(url)).json()
      if (p && p.pct != null) onPct(p.pct)
    } catch { /* a dropped poll is not worth reporting */ }
  }, interval)
  return () => clearInterval(id)
}

export const fileName = url => decodeURIComponent(url.split('/').pop())

export function fmtSize(bytes) {
  if (!bytes) return '~'
  return (bytes / 1048576).toFixed(1) + ' MB'
}
