export async function postJSON(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = await res.json()
  if (!res.ok) throw new Error(data.error || 'request failed')
  return data
}

export async function postForm(url, fd) {
  const res = await fetch(url, { method: 'POST', body: fd })
  const data = await res.json()
  if (!res.ok) throw new Error(data.error || 'request failed')
  return data
}

// Poll a /progress endpoint until stopped; returns the stop function.
export function pollProgress(url, cb) {
  const iv = setInterval(async () => {
    try {
      const p = await (await fetch(url)).json()
      if (p.pct != null) cb(p.pct)
    } catch {}
  }, 600)
  return () => clearInterval(iv)
}

export const fmtMB = b => (b ? (b / 1048576).toFixed(2) + ' MB' : '~')

export const STEM_NAMES = {
  vocals: 'Vocals', drums: 'Drums', bass: 'Bass',
  guitar: 'Guitar', piano: 'Piano', other: 'Other',
}

export const MODEL_STEMS = {
  htdemucs: ['vocals', 'drums', 'bass', 'other'],
  htdemucs_6s: ['vocals', 'drums', 'bass', 'guitar', 'piano', 'other'],
}
