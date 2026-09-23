/**
 * Tempo's wave mark — the smooth waveform that stands for rhythm and pattern.
 *
 * It appears in two places that draw very differently: the macOS menu-bar tray
 * needs a rasterised RGBA buffer, while the app chrome wants a scalable stroke.
 * Both derive from the geometry here, so the mark cannot drift between them.
 *
 * The numbers are the ones the tray has always used: a 22×22 box with two and a
 * half cycles running between 3px margins.
 */

export const WAVE = {
  /** Side of the square the wave is drawn in, and the SVG viewBox. */
  size: 22,
  centerY: 11,
  amplitude: 4,
  /** Cycles across the drawn span. */
  frequency: 2.5,
  startX: 3,
  endX: 19,
} as const;

/** Height of the wave at a given x, in the 22×22 box. */
export function waveY(x: number): number {
  const normalized = (x - WAVE.startX) / (WAVE.endX - WAVE.startX);
  return WAVE.centerY + Math.sin(normalized * Math.PI * WAVE.frequency) * WAVE.amplitude;
}

/**
 * The wave as an SVG path, sampled along its length.
 *
 * Sampling rather than fitting Béziers keeps this exactly the same curve the
 * tray rasterises — at 40 points the segments are well under a pixel at any
 * size the mark is actually drawn.
 */
export function waveSvgPath(samples = 40): string {
  const step = (WAVE.endX - WAVE.startX) / samples;
  const points: string[] = [];
  for (let index = 0; index <= samples; index += 1) {
    const x = WAVE.startX + index * step;
    points.push(`${x.toFixed(2)} ${waveY(x).toFixed(2)}`);
  }
  return `M ${points[0]} L ${points.slice(1).join(' L ')}`;
}
