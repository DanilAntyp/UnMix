import HomePage from './HomePage'
import KaraokePage from './KaraokePage'

/* Two screens, no router: Flask serves this same bundle on / and /karaoke,
   and karaoke is opened in its own window anyway. */
export default function App() {
  return location.pathname.startsWith('/karaoke') ? <KaraokePage /> : <HomePage />
}
