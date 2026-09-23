import { z } from 'zod';

export const entityTypeSchema = z.enum([
  'operation',
  'action',
  'activity',
  'goal',
  'proposition',
]);
export type EntityType = z.infer<typeof entityTypeSchema>;
export type HierarchyEntityType = Exclude<EntityType, 'proposition'>;

export const tempoStatusSchema = z.object({
  running: z.boolean(),
  error: z.string().nullable().optional(),
  message: z.string().optional(),
  started_at: z.string().nullable().optional(),
});
export type TempoStatus = z.infer<typeof tempoStatusSchema>;

export const observerDiagnosticsSchema = z.union([
  z.object({ observer: z.null(), detail: z.string() }),
  z.object({
    running: z.boolean(),
    worker_task_alive: z.boolean(),
    listener_thread_alive: z.boolean(),
    event_loop_is_running_loop: z.boolean(),
    event_loop_closed: z.boolean().nullable(),
    mouse_events: z.object({
      seen: z.number().int().nonnegative(),
      handled: z.number().int().nonnegative(),
      dropped: z.number().int().nonnegative(),
      last_drop_reason: z.string().nullable(),
    }),
    seconds_since_handled_event: z.number(),
    capture: z.object({
      frames_grabbed: z.number().int().nonnegative(),
      skipped_ignored_app_visible: z.number().int().nonnegative(),
      skipped_excluded: z.number().int().nonnegative(),
      exclusion_paused: z.boolean(),
      ignored_app_names: z.array(z.string()),
    }),
    persist: z.object({
      attempts: z.number().int().nonnegative(),
      saved: z.number().int().nonnegative(),
      skipped_low_change: z.number().int().nonnegative(),
      skipped_no_frames: z.number().int().nonnegative(),
      last_change_score: z.number().nullable(),
      change_threshold: z.number().nullable(),
      last_failure_reason: z.string().nullable(),
      blocked_by_content_filter: z.number().int().nonnegative(),
    }),
    screenshots_captured: z.number().int().nonnegative(),
    verdict: z.string(),
    health: z.object({
      state: z.enum(['starting', 'healthy', 'idle', 'paused', 'waiting', 'stalled', 'error']),
      verdict: z.string(),
      seconds_since_frame: z.number().nonnegative().nullable(),
      seconds_since_input: z.number().nonnegative().nullable(),
      oldest_pending_capture_seconds: z.number().nonnegative().nullable(),
    }),
  }),
]);
export type ObserverDiagnostics = z.infer<typeof observerDiagnosticsSchema>;

export const envConfigSchema = z.object({
  model_name: z.string(),
  provider: z.string(),
  api_key_configured: z.boolean(),
  api_base_configured: z.boolean(),
  vertex_credentials_configured: z.boolean(),
});
export type EnvConfig = z.infer<typeof envConfigSchema>;

export const installedAppSchema = z.object({
  bundle_id: z.string(),
  name: z.string(),
  icon_data_url: z.string().nullable(),
  source: z.enum(['applications', 'system', 'utilities', 'user']),
});
export type InstalledApp = z.infer<typeof installedAppSchema>;
export const installedAppsSchema = z.array(installedAppSchema);

export const exclusionSettingsSchema = z.object({
  apps: z.array(z.string()),
  domains: z.array(z.string()),
});
export type ExclusionSettings = z.infer<typeof exclusionSettingsSchema>;

export type HierarchyNode = {
  id: number;
  type: HierarchyEntityType;
  text: string;
  revision: number;
  locked: boolean;
  timestamp_start?: string | null;
  timestamp_end?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  metadata: Record<string, unknown>;
  has_children: boolean;
  children: HierarchyNode[];
};

const hierarchyEntitySchema = z.object({
  id: z.number().int(),
  type: entityTypeSchema.exclude(['proposition']),
  text: z.string(),
  revision: z.number().int().nonnegative(),
  locked: z.boolean(),
  timestamp_start: z.string().nullable(),
  timestamp_end: z.string().nullable(),
  created_at: z.string().nullable(),
  updated_at: z.string().nullable(),
  metadata: z.record(z.string(), z.unknown()),
});

export const hierarchyNodeSchema: z.ZodType<HierarchyNode> = z.lazy(() => hierarchyEntitySchema.extend({
  has_children: z.boolean(),
  children: z.array(hierarchyNodeSchema),
}));

export const hierarchyResponseSchema = z.object({
  roots: z.array(hierarchyNodeSchema),
  count: z.number().int().nonnegative(),
  max_depth: z.number().int().min(1).max(4),
});
export type HierarchyResponse = z.infer<typeof hierarchyResponseSchema>;

export const entitySummarySchema = z.object({
  id: z.number().int(),
  type: entityTypeSchema,
  text: z.string(),
  timestamp_start: z.string().nullable(),
  timestamp_end: z.string().nullable(),
});
export type EntitySummary = z.infer<typeof entitySummarySchema>;

export const searchResponseSchema = z.object({
  results: z.array(z.object({
    entity: entitySummarySchema,
    score: z.number(),
  })),
  count: z.number().int().nonnegative(),
});
export type SearchResponse = z.infer<typeof searchResponseSchema>;

export const timelineResponseSchema = z.object({
  activities: z.array(z.object({
    id: z.number().int(),
    text: z.string(),
    timestamp_start: z.string().nullable(),
    timestamp_end: z.string().nullable(),
    metadata: z.record(z.string(), z.unknown()),
  })),
  count: z.number().int().nonnegative(),
});
export type TimelineResponse = z.infer<typeof timelineResponseSchema>;

export const statsSchema = z.object({
  operations: z.number().int().nonnegative(),
  actions: z.number().int().nonnegative(),
  activities: z.number().int().nonnegative(),
  goals: z.number().int().nonnegative(),
  propositions: z.number().int().nonnegative(),
});
export type TempoStats = z.infer<typeof statsSchema>;

/**
 * Confidence reaches us however the model wrote it — `8` on one entity and
 * `"8"` on the next, both from the same prompt. Normalised here so callers see
 * a number or nothing, rather than every caller having to guess.
 */
const confidenceSchema = z
  .union([z.number(), z.string()])
  .nullable()
  .optional()
  .transform((value) => {
    if (value === null || value === undefined) return null;
    const parsed = typeof value === 'number' ? value : Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  });

export const relationSchema = z.object({
  id: z.number().int(),
  source_id: z.number().int().optional(),
  target_id: z.number().int().optional(),
  type: z.string(),
  subtype: z.string().nullable().optional(),
  confidence: confidenceSchema,
  metadata: z.record(z.string(), z.unknown()).nullable().optional(),
  source_text: z.string().optional(),
  target_text: z.string().optional(),
  source_type: entityTypeSchema.optional(),
  target_type: entityTypeSchema.optional(),
}).passthrough();
export type Relation = z.infer<typeof relationSchema>;
export const entitySchema = z.object({
  id: z.number().int(),
  type: entityTypeSchema,
  text: z.string(),
  confidence: confidenceSchema,
  timestamp_start: z.string().nullable(),
  timestamp_end: z.string().nullable(),
  created_at: z.string().nullable().optional(),
  timestamp: z.string().nullable().optional(),
  metadata: z.record(z.string(), z.unknown()),
  relations: z.object({
    outgoing: z.array(relationSchema),
    incoming: z.array(relationSchema),
  }).optional(),
}).passthrough();
export type Entity = z.infer<typeof entitySchema>;

export const entitiesResponseSchema = z.object({
  entities: z.array(entitySchema),
  count: z.number().int().nonnegative(),
  total: z.number().int().nonnegative().optional(),
});

export const screenshotsResponseSchema = z.object({
  entity_id: z.number().int(),
  screenshots: z.array(z.object({
    path: z.string(),
    url: z.string(),
    timestamp: z.string().nullable(),
    entity_id: z.number().int(),
    entity_text: z.string(),
  })),
});
export type EntityScreenshot = z.infer<typeof screenshotsResponseSchema>['screenshots'][number];

export const actionResponseSchema = z.object({
  status: z.string(),
  message: z.string(),
});

export type StartRequest = {
  model_name?: string;
  platform: 'macos' | 'gnome';
  api_key?: string;
  api_base?: string;
  gemini_vertexai_express?: boolean;
  debug?: boolean;
};

export type EntityUpdateRequest = {
  expected_revision: number;
  text?: string;
  locked?: boolean;
};

export type EntityReparentRequest = {
  new_parent_id: number;
  expected_revision: number;
};

export type EntityMergeRequest = {
  ids: number[];
  text?: string;
  expected_revisions: Record<number, number>;
};

export type EntitySplitRequest = {
  expected_revision: number;
  into: Array<{ text: string; child_ids: number[] }>;
};

export type EntityDeleteRequest = {
  expected_revision: number;
  reparent_children_to?: number;
};

export type EntityCreateRequest = {
  type: HierarchyEntityType;
  text: string;
  parent_id?: number;
};

export const entityMutationResponseSchema = z.object({ entity: hierarchyNodeSchema });
export const reparentResponseSchema = entityMutationResponseSchema.extend({
  old_parent_ids: z.array(z.number().int()),
});
export const mergeResponseSchema = entityMutationResponseSchema.extend({
  merged_ids: z.array(z.number().int()),
});
export const splitResponseSchema = z.object({
  entities: z.array(hierarchyNodeSchema),
  split_from: z.number().int(),
});
export const deleteResponseSchema = z.object({
  deleted: hierarchyEntitySchema,
  children: z.array(hierarchyNodeSchema),
});

// ── Onboarding ──────────────────────────────────────────────────────────────

export const onboardingQuestionSchema = z.object({
  key: z.string(),
  label: z.string(),
  prompt: z.string(),
  placeholder: z.string(),
});
export type OnboardingQuestion = z.infer<typeof onboardingQuestionSchema>;

export const onboardingSchema = z.object({
  questions: z.array(onboardingQuestionSchema),
  user_name: z.string(),
  responses: z.record(z.string(), z.string()),
  completed: z.boolean(),
  dismissed: z.boolean(),
});
export type Onboarding = z.infer<typeof onboardingSchema>;

export const onboardingSaveSchema = z.object({
  saved: z.boolean(),
  applied_to_running_pipeline: z.boolean(),
  answered: z.number().int().nonnegative(),
  dismissed: z.boolean(),
});

export type OnboardingSaveRequest = {
  user_name: string;
  responses: Record<string, string>;
  /** True when the prompt was closed without answering. */
  dismissed?: boolean;
};

// ── Hierarchy compile ───────────────────────────────────────────────────────

/** The user's staged markup of the tree, posted when they hit Compile. */
export type HierarchyCompileRequest = {
  text_overrides?: Record<number, string>;
  rejected_ids?: number[];
  locked_ids?: number[];
  goal_merges?: Array<[number, number]>;
  activity_merges?: Array<[number, number]>;
  removed_action_relations?: string[];
  removed_action_ids?: number[];
  reparents?: Array<[number, number]>;
  annotations?: Array<{ entity_id: number; type: string; text: string }>;
};

const compileAppliedSchema = z.object({
  text_edits: z.number().int().nonnegative(),
  rejections: z.number().int().nonnegative(),
  locks: z.number().int().nonnegative(),
  merges: z.number().int().nonnegative(),
  action_removals: z.number().int().nonnegative(),
  reparents: z.number().int().nonnegative(),
  annotations: z.number().int().nonnegative(),
});
export type CompileApplied = z.infer<typeof compileAppliedSchema>;

const compileChangeSchema = z.object({
  id: z.number().int(),
  type: entityTypeSchema.exclude(['proposition']),
  text: z.string(),
  previous_text: z.string().optional(),
  previous_parent_id: z.number().int().nullable().optional(),
  parent_id: z.number().int().nullable().optional(),
});
export type CompileChange = z.infer<typeof compileChangeSchema>;

export const compileResultSchema = z.object({
  token: z.string(),
  synthesized: z.boolean(),
  llm_calls: z.number().int().nonnegative().default(0),
  applied: compileAppliedSchema,
  before: z.array(hierarchyNodeSchema),
  after: z.array(hierarchyNodeSchema),
  highlights: z.record(z.string(), z.enum(['added', 'modified'])),
  added: z.array(compileChangeSchema),
  modified: z.array(compileChangeSchema),
  removed: z.array(compileChangeSchema),
  counts: z.object({
    added: z.number().int().nonnegative(),
    modified: z.number().int().nonnegative(),
    removed: z.number().int().nonnegative(),
  }),
});
export type CompileResult = z.infer<typeof compileResultSchema>;

/** One frame of the compile SSE stream. */
export type CompileEvent =
  | {
      type: 'progress';
      step: string;
      detail: string;
      /** Model calls started so far — only meaningful during re-synthesis. */
      llm_calls?: number;
      elapsed_seconds?: number;
    }
  | { type: 'complete'; data: CompileResult }
  | { type: 'error'; error: string };

export const compileEventSchema: z.ZodType<CompileEvent> = z.discriminatedUnion('type', [
  z.object({
    type: z.literal('progress'),
    step: z.string(),
    detail: z.string(),
    llm_calls: z.number().int().nonnegative().optional(),
    elapsed_seconds: z.number().nonnegative().optional(),
  }),
  z.object({ type: z.literal('complete'), data: compileResultSchema }),
  z.object({ type: z.literal('error'), error: z.string() }),
]);

export const compileDispositionSchema = z.object({
  token: z.string(),
  accepted_at: z.string().optional(),
  reverted_at: z.string().optional(),
  backup: z.string().optional(),
}).passthrough();
export type CompileDisposition = z.infer<typeof compileDispositionSchema>;

export type ServerEvent =
  | { type: 'connection'; connected: boolean }
  | { type: 'status'; data: TempoStatus }
  | { type: 'hierarchy'; action: string; data: unknown }
  | { type: 'pong' };

export const serverEventSchema: z.ZodType<ServerEvent> = z.discriminatedUnion('type', [
  z.object({ type: z.literal('connection'), connected: z.boolean() }),
  z.object({ type: z.literal('status'), data: tempoStatusSchema }),
  z.object({ type: z.literal('hierarchy'), action: z.string(), data: z.unknown() }),
  z.object({ type: z.literal('pong') }),
]);
