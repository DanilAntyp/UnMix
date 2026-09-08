import { ArrowUpRight, Sparkles } from 'lucide-react'

// CSS illumination stays inexpensive and follows the workspace motion setting.
export function LiquidMetalButton({
  label = 'Get Started',
  onClick,
  viewMode = 'text',
  width,
  disabled = false,
}) {
  return (
    <button
      className="btn-primary"
      style={width ? { minWidth: width } : undefined}
      onClick={onClick}
      disabled={disabled}
    >
      <span>{viewMode === 'icon' ? <Sparkles size={17} /> : label}</span>
      {viewMode !== 'icon' && <ArrowUpRight size={16} aria-hidden="true" />}
    </button>
  )
}
