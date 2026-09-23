import { WAVE, waveSvgPath } from '../../shared/wave-mark';

/**
 * Tempo's wave mark, drawn from the same geometry the menu-bar tray rasterises.
 *
 * Strokes in `currentColor`, so it takes the colour of whatever it sits in —
 * dark on the light app chrome, light on a dark surface.
 */
export default function TempoMark({
  size = 18,
  strokeWidth = 2,
  className,
  title,
}: {
  size?: number;
  strokeWidth?: number;
  className?: string;
  /** Give this only where the mark is the sole label for a control. */
  title?: string;
}) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox={`0 0 ${WAVE.size} ${WAVE.size}`}
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      role={title ? 'img' : undefined}
      aria-hidden={title ? undefined : true}
      aria-label={title}
    >
      {title && <title>{title}</title>}
      <path d={waveSvgPath()} />
    </svg>
  );
}
