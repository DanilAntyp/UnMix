/* Stem metadata shared by the separation panel and the karaoke page.
   Colors double as the accent for each stem's result card. */
export const STEMS = {
  vocals: { icon: '\u{1F3A4}', label: 'Vocals', color: '#ff2e9a' },
  drums:  { icon: '\u{1F941}', label: 'Drums', color: '#ff7a1a' },
  bass:   { icon: '\u{1F3B8}', label: 'Bass', color: '#ffc233' },
  guitar: { icon: '\u{1F3B6}', label: 'Guitar', color: '#3ef2b5' },
  piano:  { icon: '\u{1F3B9}', label: 'Piano', color: '#22e4ff' },
  other:  { icon: '\u{1F3BC}', label: 'Other', color: '#8b5cff' },
}

export const MODEL_STEMS = {
  htdemucs: ['vocals', 'drums', 'bass', 'other'],
  htdemucs_6s: ['vocals', 'drums', 'bass', 'guitar', 'piano', 'other'],
}

export const stemLabel = k => (STEMS[k] ? `${STEMS[k].icon} ${STEMS[k].label}` : k)
