// Pure helpers for the chat composer's "Send" pipeline (Query Workbench Shell,
// §3.2). Send always generates the source query and transpiles it; it executes
// only when connected. Keeping this logic pure makes the degradation table
// testable without a DOM/React harness.

export interface SendIntent {
  /** Run NL -> source -> transpile after generation. Always true for Send. */
  translate: boolean;
  /** Execute after a successful transpile. Only when connected. */
  run: boolean;
}

/**
 * Decide what a Send should do given connection state.
 *
 * - Connected: generate -> transpile -> execute.
 * - Disconnected: generate -> transpile only (results need a live DB).
 */
export function planSend(connected: boolean): SendIntent {
  return { translate: true, run: connected };
}

export const IDLE_INTENT: SendIntent = { translate: false, run: false };

export interface PipelineFlags {
  nlLoading: boolean;
  translating: boolean;
  executing: boolean;
}

export type PipelineStageId =
  | "idle"
  | "generating"
  | "transpiling"
  | "running";

/**
 * The single in-progress stage, derived from the app's busy flags. Returns
 * ``"idle"`` when nothing is running. Generation is checked first because the
 * stages run in order (generate -> transpile -> run).
 */
export function currentStage(flags: PipelineFlags): PipelineStageId {
  if (flags.nlLoading) return "generating";
  if (flags.translating) return "transpiling";
  if (flags.executing) return "running";
  return "idle";
}

const STAGE_LABELS: Record<PipelineStageId, string> = {
  idle: "",
  generating: "Generating Cypher…",
  transpiling: "Transpiling to AQL…",
  running: "Running…",
};

export function stageLabel(stage: PipelineStageId): string {
  return STAGE_LABELS[stage];
}

export function isBusy(flags: PipelineFlags): boolean {
  return flags.nlLoading || flags.translating || flags.executing;
}
