/* Monochrome line icons — emoji render differently on every platform and
   fight with the gradient tiles they sit on. */
const PATHS = {
  download: 'M12 3v12m0 0 4.5-4.5M12 15l-4.5-4.5M4 17.5V19a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-1.5',
  bolt: 'M13 2 4.5 13.5H11l-1 8.5L19.5 10H13l0-8Z',
  scissors: 'M6 4l12 15M18 4 6 19M8 20a2.6 2.6 0 1 0 0-5.2 2.6 2.6 0 0 0 0 5.2ZM16 20a2.6 2.6 0 1 0 0-5.2 2.6 2.6 0 0 0 0 5.2Z',
  note: 'M9 18V5l11-2v13M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm11-2a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z',
  mic: 'M12 3a3 3 0 0 1 3 3v6a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3ZM5 11a7 7 0 0 0 14 0M12 18v3',
}

export default function Icon({ name, size = 20, stroke = 1.9, fill = 'none' }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill={fill}
         stroke="currentColor" strokeWidth={stroke}
         strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[name]} />
    </svg>
  )
}
