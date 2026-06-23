// Pure helpers for the AQL-pane warnings strip (translate/execute warnings such
// as the unlabeled-MATCH side-store notice). These warnings are surfaced in a
// single dismissible location; dismissals apply to the current warning set and
// are cleared when a fresh translate/execute produces a different set.

export interface TranslateWarning {
  message: string;
}

// Stable identity for a set of warnings. When this changes, prior dismissals
// should be cleared so genuinely new warnings reappear. Uses a control
// character as the separator so it cannot collide with message text.
export function warningsKey(warnings: TranslateWarning[]): string {
  return warnings.map((w) => w.message).join("\u0001");
}

// Filter out warnings the user has explicitly dismissed.
export function filterVisibleWarnings<T extends TranslateWarning>(
  warnings: T[],
  dismissed: ReadonlySet<string>,
): T[] {
  return warnings.filter((w) => !dismissed.has(w.message));
}
