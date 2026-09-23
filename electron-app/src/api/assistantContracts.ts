import { z } from 'zod';

export const assistantGoalSchema = z.object({
  id: z.number().int().positive(),
  text: z.string(),
  revision: z.number().int().nonnegative(),
  user_edited: z.boolean(),
});
export type AssistantGoal = z.infer<typeof assistantGoalSchema>;
export const assistantGoalsSchema = z.object({ goals: z.array(assistantGoalSchema) });
const assistantEvidenceSchema = z.object({
  id: z.number().int().positive(),
  type: z.enum(['activity', 'action', 'operation']),
  text: z.string(),
  timestamp_start: z.string(),
  timestamp_end: z.string().nullable(),
  goal_ids: z.array(z.number().int().positive()),
});
export type AssistantEvidence = z.infer<typeof assistantEvidenceSchema>;
export const assistantResponseSchema = z.object({
  summary: z.string(),
  options: z.array(z.object({
    id: z.string(),
    goal_ids: z.array(z.number().int().positive()).min(1).max(3),
    action: z.string(),
    why: z.string(),
    evidence_ids: z.array(z.number().int().positive()).min(1).max(6),
    edge_ids: z.array(z.number().int().positive()).max(8),
    confidence: z.number().int().min(8).max(10),
    expires_at: z.string(),
  })).max(3),
  tradeoffs: z.array(z.object({
    goal_ids: z.array(z.number().int().positive()).min(2).max(3),
    description: z.string(),
    evidence_ids: z.array(z.number().int().positive()).min(2).max(6),
  })).max(3),
  context_id: z.string(),
  generated_at: z.string(),
  goals: z.array(assistantGoalSchema),
  evidence: z.array(assistantEvidenceSchema),
});
export type AssistantResponse = z.infer<typeof assistantResponseSchema>;
export type AssistantRequest = { goal_ids: number[] | null; situation: string };

export const assistantFeedSchema = z.object({
  enabled: z.boolean(),
  busy: z.boolean(),
  error: z.string().nullable(),
  connection: z.object({ id: z.string(), model: z.string(), destination: z.string(), label: z.string(), configured: z.boolean() }),
  focus_goal_ids: z.array(z.number().int().positive()).nullable(),
  user_note: z.string(),
  context: z.object({
    id: z.string(),
    summary: z.string(),
    observed_at: z.string().nullable(),
    evidence_ids: z.array(z.number().int().positive()),
  }),
  goals: z.array(assistantGoalSchema),
  lenses: z.array(z.object({
    goal_id: z.number().int().positive(),
    why: z.string(),
    evidence_ids: z.array(z.number().int().positive()).min(1),
  })).max(3),
  advice: assistantResponseSchema.nullable(),
});
export type AssistantFeed = z.infer<typeof assistantFeedSchema>;
export type AssistantRefineRequest = AssistantRequest & { context_id: string | null };
export type AssistantFeedbackRequest = { suggestion_id: string; status: 'dismissed' | 'completed' | 'discussed' };

export const chatFocusSchema = z.object({
  kind: z.enum(['overview', 'goal', 'activity', 'action', 'operation', 'suggestion', 'tradeoff']),
  title: z.string().min(1).max(300),
  detail: z.string().max(1500),
  entity_ids: z.array(z.number().int().positive()).max(8),
});
export type ChatFocus = z.infer<typeof chatFocusSchema>;
export type ChatIntent = 'reflect' | 'suggest' | 'prepare';
export const chatMessageSchema = z.object({
  role: z.enum(['user', 'assistant']),
  content: z.string().min(1).max(8000),
  timestamp: z.string(),
  focus: chatFocusSchema,
  strategy: z.string().nullable(),
});
export type ChatMessage = z.infer<typeof chatMessageSchema>;
export const chatReplySchema = z.object({
  session_id: z.string().uuid(),
  revision: z.number().int().nonnegative(),
  message: chatMessageSchema.extend({ role: z.literal('assistant') }),
  entity_refs: z.array(z.number().int().positive()),
});
export type ChatReply = z.infer<typeof chatReplySchema>;
export type ChatSendRequest = {
  request_id: string;
  session_id: string | null;
  expected_revision: number;
  context_id: string | null;
  message: string;
  intent: ChatIntent;
  focus: ChatFocus;
};
export const chatHistorySchema = z.object({
  session_id: z.string().uuid(), revision: z.number().int().nonnegative(), messages: z.array(chatMessageSchema),
});
export type ChatHistory = z.infer<typeof chatHistorySchema>;
export const chatThreadsSchema = z.object({
  threads: z.array(z.object({
    session_id: z.string().uuid(), title: z.string(), updated_at: z.string(), focus: chatFocusSchema,
  })),
});
export type ChatThread = z.infer<typeof chatThreadsSchema>['threads'][number];
