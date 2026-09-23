/**
 * Per-level design tokens for the card editor.
 *
 * Hierarchy colours share the Soft website palette in styles/main.css.
 * Labels use darker ink than the node fills so small text stays readable.
 */

export type HierarchyLevel = 'goal' | 'activity' | 'action' | 'operation';

export type LevelTokens = {
  /** Type and dot colour for the level's label row. */
  accent: string;
  /** Resting card border. */
  border: string;
  /** Resting card background. */
  background: string;
  /** Connector line colour drawn down to this level's children. */
  line: string;
};

export const LEVEL_TOKENS: Record<HierarchyLevel, LevelTokens> = {
  goal:      { accent: 'var(--level-goal)', border: 'var(--level-goal)', background: 'var(--panel)', line: 'var(--line-strong)' },
  activity:  { accent: 'var(--level-activity)', border: 'var(--line-strong)', background: 'var(--surface-activity)', line: 'var(--line)' },
  action:    { accent: 'var(--level-action)', border: 'var(--line)', background: 'var(--surface-action)', line: 'var(--line)' },
  operation: { accent: 'var(--level-operation)', border: 'var(--line)', background: 'var(--surface-operation)', line: 'var(--line-soft)' },
};

/** Card widths, which the connector bars need in order to span their row. */
export const CARD_WIDTH = {
  goal: 260,
  activity: 236,
  action: 230,
} as const;

/** Gutters between siblings. Wide enough to clear the merge checkbox, which
 *  hangs off each card's right edge without taking space in the flow. */
export const ROW_GAP = {
  goal: 44,
  activity: 34,
} as const;

/** Highlight colours for entities the compile changed. */
export const HIGHLIGHT = {
  added:    { border: '#22c55e', glow: 'rgba(34,197,94,0.20)', ink: '#166534', wash: '#dcfce7' },
  modified: { border: '#3b82f6', glow: 'rgba(59,130,246,0.18)', ink: '#1e40af', wash: '#dbeafe' },
} as const;

/** Cards staged for a merge share a violet treatment on both sides of the pair. */
export const MERGE_TINT = {
  border: '#a78bfa',
  glow: 'rgba(167,139,250,0.15)',
  ink: '#7c3aed',
  wash: '#FAF8FF',
} as const;

/**
 * The same four node colours as the website's interactive graph. Outlines
 * keep the lighter operation nodes visible against white at small sizes.
 */
export const PAPER_PALETTE: Record<HierarchyLevel, { fill: string; stroke: string; text: string }> = {
  goal:      { fill: 'var(--node-goal)', stroke: 'var(--node-goal-stroke)', text: 'var(--ink)' },
  activity:  { fill: 'var(--node-activity)', stroke: 'var(--node-activity-stroke)', text: 'var(--ink)' },
  action:    { fill: 'var(--node-action)', stroke: 'var(--node-action-stroke)', text: 'var(--ink)' },
  operation: { fill: 'var(--node-operation)', stroke: 'var(--node-operation-stroke)', text: 'var(--ink)' },
};

/** Ink used for connectors and labels in the figure. */
export const PAPER_INK = 'var(--ink)';

/** The figure's accent for a striving the system induced from user edits. */
export const PAPER_ACCENT = '#7da875';

/**
 * How each relation kind is drawn. Structural links are the hierarchy's spine
 * and read as solid; everything else is a claim *about* the hierarchy rather
 * than part of it, so those are dashed and coloured by what they assert.
 */
export const EDGE_STYLES: Record<string, { color: string; dash?: string; label: string; width: number }> = {
  part_of:   { color: '#564836', label: 'part of',    width: 1.6 },
  same_as:   { color: '#8a7a63', label: 'same as',    width: 1.4, dash: '2 3' },
  follows:   { color: '#9c8f7d', label: 'follows',    width: 1.2, dash: '5 4' },
  overlaps:  { color: '#a89478', label: 'overlaps',   width: 1.2, dash: '5 4' },
  co_occurs: { color: '#b0a48f', label: 'co-occurs', width: 1.2, dash: '1 4' },
  competes:  { color: '#c0603f', label: 'competes',   width: 1.4, dash: '6 3' },
  supersedes:{ color: '#7d6a51', label: 'supersedes', width: 1.4, dash: '8 3' },
  supports:  { color: '#7da875', label: 'supports',   width: 1.6, dash: '6 3' },
  hinders:   { color: '#c0603f', label: 'hinders',    width: 1.6, dash: '6 3' },
  maintains: { color: '#8fa88a', label: 'maintains',  width: 1.4, dash: '4 3' },
  modifies_determinant: { color: '#9c8f7d', label: 'modifies', width: 1.2, dash: '2 3' },
};

export const JUDGMENT_TINT = {
  approve: { ink: '#2E7D32', wash: '#E8F5E9', label: 'accepted' },
  reject:  { ink: '#C62828', wash: '#FFEBEE', label: 'removed' },
} as const;
