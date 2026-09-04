/**
 * Pure selection logic shared between the atom that holds selection state
 * and every component that reads or mutates it.
 *
 * A selection is a predicate (a folder-wide "select all" scope, minted
 * server-side) plus two explicit id sets layered on top of it: `included`
 * and `excluded`. With no predicate, `included` is the whole selection --
 * this is what a hand-picked set of checkboxes already is today. With a
 * predicate, a row that matches it is selected unless named in `excluded`;
 * a row that doesn't match it (mail arrived after the predicate's snapshot,
 * for instance) is selected only if named in `included`. Nothing here
 * needs the full id list a predicate covers -- unmounting and remounting a
 * row during a scroll can never lose anything, because membership is
 * re-derived from the predicate every time a row renders.
 */

export interface SelectionPredicate {
  accountId: string;
  folderId: string;
  filter: "all" | "unread";
  /** Server-minted (GET .../messages/selection). A message mirrored after
   * this instant is outside the selection, however it later reads. */
  snapshotAt: string;
  /** The server's count of the predicate at snapshot time, before exclusions. */
  count: number;
}

export interface SelectableRow {
  id: string;
  folder_id: string;
  is_seen: boolean;
  /** Absent on a row type that predicate mode is never offered against
   * (the unified/threaded views) -- treated as never matching a predicate. */
  mirrored_at?: string;
}

export interface SelectionState {
  predicate: SelectionPredicate | null;
  included: ReadonlySet<string>;
  excluded: ReadonlySet<string>;
  /** Shift-range anchor: the row a plain or ctrl click last landed on. */
  anchorId: string | null;
  /** The included/excluded sets exactly as they were the instant the
   * anchor was set -- a shift-click always recomputes from this, never
   * from whatever a previous shift-click left behind, or a nearer
   * shift-click would leave a stale selected tail beyond the new target. */
  anchorBase: { included: ReadonlySet<string>; excluded: ReadonlySet<string> } | null;
}

export const EMPTY_SELECTION: SelectionState = {
  predicate: null,
  included: new Set(),
  excluded: new Set(),
  anchorId: null,
  anchorBase: null,
};

/** Whether a row falls inside a predicate's scope, independent of exclusions. */
export function matchesPredicate(p: SelectionPredicate, row: SelectableRow): boolean {
  if (row.folder_id !== p.folderId) return false;
  if (p.filter === "unread" && row.is_seen) return false;
  if (!row.mirrored_at) return false;
  return Date.parse(row.mirrored_at) <= Date.parse(p.snapshotAt);
}

/** Whether a row is currently ticked, under either mode. */
export function isRowSelected(s: SelectionState, row: SelectableRow): boolean {
  if (!s.predicate) return s.included.has(row.id);
  if (matchesPredicate(s.predicate, row)) return !s.excluded.has(row.id);
  return s.included.has(row.id);
}

/** The selection's total count, derived rather than tracked separately. */
export function selectionSize(s: SelectionState): number {
  if (!s.predicate) return s.included.size;
  return s.predicate.count - s.excluded.size + s.included.size;
}

/** Set one row's ticked state, the way whichever mode is active expresses it. */
function setRowChecked(s: SelectionState, row: SelectableRow, checked: boolean): SelectionState {
  if (!s.predicate || !matchesPredicate(s.predicate, row)) {
    const included = new Set(s.included);
    if (checked) included.add(row.id);
    else included.delete(row.id);
    return { ...s, included };
  }
  const excluded = new Set(s.excluded);
  if (checked) excluded.delete(row.id);
  else excluded.add(row.id);
  return { ...s, excluded };
}

/**
 * Checkbox click / ctrl+click on a row's text: toggle that one row and set
 * it as the shift-range anchor, capturing the resulting state as
 * `anchorBase` for any shift-click that follows.
 */
export function toggleRow(s: SelectionState, row: SelectableRow): SelectionState {
  const next = setRowChecked(s, row, !isRowSelected(s, row));
  return {
    ...next,
    anchorId: row.id,
    anchorBase: { included: new Set(next.included), excluded: new Set(next.excluded) },
  };
}

/**
 * Shift-click: extend from the anchor to `targetId` over `visibleIds` (the
 * loaded, in-order id list -- exact because the fetch window is append-only
 * from the top, so any two rendered rows have every row between them
 * loaded). Every row in the range takes on the anchor's own resulting
 * ticked state, computed once from `anchorBase` -- not each row's own
 * predicate membership -- so an "exclude" anchor excludes the whole range
 * and an "include" anchor includes it, with no per-row special case.
 *
 * With no anchor yet (the very first gesture), a shift-click behaves like
 * an ordinary toggle on the target row.
 */
export function extendRange(
  s: SelectionState,
  visibleIds: string[],
  rowsById: ReadonlyMap<string, SelectableRow>,
  targetId: string,
): SelectionState {
  const targetRow = rowsById.get(targetId);
  if (!targetRow) return s;
  if (!s.anchorId || !s.anchorBase) return toggleRow(s, targetRow);

  const fromIdx = visibleIds.indexOf(s.anchorId);
  const toIdx = visibleIds.indexOf(targetId);
  if (fromIdx === -1 || toIdx === -1) return s;
  const [start, end] = fromIdx < toIdx ? [fromIdx, toIdx] : [toIdx, fromIdx];
  const range = visibleIds.slice(start, end + 1);

  const anchorRow = rowsById.get(s.anchorId);
  const anchorBaseState: SelectionState = {
    ...s,
    included: s.anchorBase.included,
    excluded: s.anchorBase.excluded,
  };
  const anchorChecked = anchorRow ? isRowSelected(anchorBaseState, anchorRow) : false;

  let next: SelectionState = {
    ...s,
    included: new Set(s.anchorBase.included),
    excluded: new Set(s.anchorBase.excluded),
  };
  for (const id of range) {
    const row = rowsById.get(id);
    if (row) next = setRowChecked(next, row, anchorChecked);
  }
  return { ...next, anchorId: s.anchorId, anchorBase: s.anchorBase };
}
