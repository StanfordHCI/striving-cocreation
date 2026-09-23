// Entity-type display helpers shared by retained UI components.

export const TYPE_LABELS = {
  goal: { singular: 'goal', plural: 'goals', title: 'Goal', titlePlural: 'Goals' },
  activity: { singular: 'activity', plural: 'activities', title: 'Activity', titlePlural: 'Activities' },
  action: { singular: 'task', plural: 'tasks', title: 'Task', titlePlural: 'Tasks' },
  operation: { singular: 'action', plural: 'actions', title: 'Action', titlePlural: 'Actions' },
  proposition: { singular: 'proposition', plural: 'propositions', title: 'Proposition', titlePlural: 'Propositions' },
} as const;

type TypeLabelKey = keyof typeof TYPE_LABELS;

function hasTypeLabel(type: string): type is TypeLabelKey {
  return type in TYPE_LABELS;
}

export function typeTitle(type: string): string {
  return hasTypeLabel(type) ? TYPE_LABELS[type].title : type;
}

export function typeTitlePlural(type: string): string {
  return hasTypeLabel(type) ? TYPE_LABELS[type].titlePlural : `${type}s`;
}

export function typePluralFromStatsKey(statsKey: string): string {
  // Stats keys are backend-oriented: operations/actions/activities
  if (statsKey === 'goals') return 'goals';
  if (statsKey === 'activities') return 'activities';
  if (statsKey === 'actions') return 'tasks';
  if (statsKey === 'operations') return 'observations';
  if (statsKey === 'propositions') return 'propositions';
  return statsKey;
}

// ---------------------------------------------------------------------------
// Core entity-type union and tuple
// ---------------------------------------------------------------------------

/** The four canonical entity types used throughout the app. */
export type EntityType = 'operation' | 'action' | 'activity' | 'goal';

/**
 * Tuple of all entity types in hierarchy order (atomic → motive-driven).
 * Mirrors the Activity Theory hierarchy: Operations → Actions → Activities,
 * with Goals sitting above as the motive layer.
 */
export const ENTITY_TYPES: readonly EntityType[] = [
  'operation',
  'action',
  'activity',
  'goal',
] as const;

// ---------------------------------------------------------------------------
// Type guards (replace inline `e.type === 'X'` checks)
// ---------------------------------------------------------------------------

export const isOperation = (e: { type?: string } | null | undefined): boolean =>
  !!e && e.type === 'operation';

export const isAction = (e: { type?: string } | null | undefined): boolean =>
  !!e && e.type === 'action';

export const isActivity = (e: { type?: string } | null | undefined): boolean =>
  !!e && e.type === 'activity';

export const isGoal = (e: { type?: string } | null | undefined): boolean =>
  !!e && e.type === 'goal';

/** True if the value is one of the four canonical EntityTypes. */
export const isEntityType = (type: unknown): type is EntityType =>
  typeof type === 'string' && (ENTITY_TYPES as readonly string[]).includes(type);

// ---------------------------------------------------------------------------
// Display: icons
// ---------------------------------------------------------------------------

/**
 * Icon glyphs per hierarchy entity type.
 */
export const entityIcon: Record<EntityType, string> = {
  goal: '★',
  activity: '◉',
  action: '◈',
  operation: '○',
};

/** Convenience accessor with fallback for unknown / non-canonical types. */
export function iconForType(type: string | undefined | null): string {
  if (type && isEntityType(type)) return entityIcon[type];
  return '•';
}

// ---------------------------------------------------------------------------
// Display: colors
// ---------------------------------------------------------------------------

/**
 * Shared hex colors per hierarchy entity type.
 */
export const entityColor: Record<EntityType, string> = {
  goal: '#f59e0b',
  activity: '#3b82f6',
  action: '#a855f7',
  operation: '#4ade80',
};

/** Convenience accessor with neutral fallback for unknown types. */
export function colorForType(type: string | undefined | null): string {
  if (type && isEntityType(type)) return entityColor[type];
  return '#6b7280';
}
