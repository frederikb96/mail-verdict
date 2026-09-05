/**
 * Pure selection logic shared between the atom that holds selection state
 * and every component that reads or mutates it.
 *
 * A selection is a predicate (a folder-wide "select all" scope, minted
 * server-side) plus two explicit id maps layered on top of it: `included`
 * and `excluded`, each mapping an id to the account it belongs to. With no
 * predicate, `included` is the whole selection -- this is what a hand-picked
 * set of checkboxes already is today. With a predicate, a row that matches
 * it is selected unless named in `excluded`; a row that doesn't match it
 * (mail arrived after the predicate's snapshot, for instance) is selected
 * only if named in `included`. Nothing here needs the full id list a
 * predicate covers -- unmounting and remounting a row during a scroll can
 * never lose anything, because membership is re-derived from the predicate
 * every time a row renders.
 *
 * A selection also carries the identity of the list it was made in --
 * `scope`. It is only ever meaningful for the list it was made against: a
 * predicate is scoped to one folder by construction, and an explicit pick
 * is scoped the same way so it can't silently apply to whatever folder is
 * on screen once the reader has navigated on. See `selectionForScope`.
 */

export interface SelectionScope {
  /** A real account id, or the literal "unified" for the merged view. */
  accountId: string;
  /** A real folder id in single-account mode, or the unified folder name. */
  folderId: string;
  /** Threading only exists in single-account mode; always false for the
   * unified view regardless of the (persisted, view-only) threaded toggle. */
  threaded: boolean;
}

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
  account_id: string;
  folder_id: string;
  is_seen: boolean;
  /** Absent on a row type that predicate mode is never offered against
   * (the unified/threaded views) -- treated as never matching a predicate. */
  mirrored_at?: string;
}

export interface SelectionState {
  predicate: SelectionPredicate | null;
  /** Ticked ids, each recording the account it belongs to at the moment it
   * was ticked -- resolving a bulk action never re-derives this from a
   * list cache later, which may have evicted or moved the row by then. */
  included: ReadonlyMap<string, string>;
  excluded: ReadonlyMap<string, string>;
  /** Shift-range anchor: the row a plain or ctrl click last landed on. */
  anchorId: string | null;
  /** The included/excluded maps exactly as they were the instant the
   * anchor was set -- a shift-click always recomputes from this, never
   * from whatever a previous shift-click left behind, or a nearer
   * shift-click would leave a stale selected tail beyond the new target. */
  anchorBase: { included: ReadonlyMap<string, string>; excluded: ReadonlyMap<string, string> } | null;
  /** The list this selection was made against. Null only for
   * EMPTY_SELECTION, which has nothing to be scoped to and matches
   * whatever list is on screen. */
  scope: SelectionScope | null;
}

export const EMPTY_SELECTION: SelectionState = {
  predicate: null,
  included: new Map(),
  excluded: new Map(),
  anchorId: null,
  anchorBase: null,
  scope: null,
};

function scopesEqual(a: SelectionScope, b: SelectionScope): boolean {
  return a.accountId === b.accountId && a.folderId === b.folderId && a.threaded === b.threaded;
}

/**
 * A selection as it applies to `scope` -- itself once a mismatch means
 * nothing has changed, otherwise treated as if it had already been
 * cleared. This is the one place that decides whether a selection still
 * describes what's on screen; every reader and every gesture goes through
 * it rather than recomputing the same comparison at its own call site.
 */
export function selectionForScope(s: SelectionState, scope: SelectionScope): SelectionState {
  if (s.scope === null || scopesEqual(s.scope, scope)) return s;
  return EMPTY_SELECTION;
}

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
    const included = new Map(s.included);
    if (checked) included.set(row.id, row.account_id);
    else included.delete(row.id);
    return { ...s, included };
  }
  const excluded = new Map(s.excluded);
  if (checked) excluded.delete(row.id);
  else excluded.set(row.id, row.account_id);
  return { ...s, excluded };
}

/**
 * Checkbox click / ctrl+click on a row's text: toggle that one row and set
 * it as the shift-range anchor, capturing the resulting state as
 * `anchorBase` for any shift-click that follows. `scope` is the list this
 * gesture is happening in -- a stale selection made in a different one is
 * discarded first, so the toggle always starts from a selection that
 * actually describes what's on screen.
 */
export function toggleRow(s: SelectionState, row: SelectableRow, scope: SelectionScope): SelectionState {
  const scoped = selectionForScope(s, scope);
  const next = setRowChecked(scoped, row, !isRowSelected(scoped, row));
  return {
    ...next,
    scope,
    anchorId: row.id,
    anchorBase: { included: new Map(next.included), excluded: new Map(next.excluded) },
  };
}

/**
 * Shift-click: extend from the anchor to `targetId` over `visibleIds` (the
 * loaded, in-order id list -- exact because the fetch window is append-only
 * from the top, so any two rendered rows have every row between them
 * loaded). A shift-click always *selects* the range, never deselects it --
 * Outlook and Gmail both treat it as "extend the selection to here"
 * unconditionally, and a person who unticked a row with ctrl-click and then
 * shift-clicks is reaching for more rows, not fewer. Deselecting a block
 * stays available through ctrl-click on each row, or clearing entirely.
 * Recomputed once from `anchorBase` on every shift-click -- never from
 * whatever a previous shift-click left behind, or a nearer one would leave
 * a stale selected tail beyond the new target.
 *
 * With no anchor yet (the very first gesture), a shift-click selects the
 * target row on its own, the same as any other shift-click's range of one.
 */
export function extendRange(
  s: SelectionState,
  visibleIds: string[],
  rowsById: ReadonlyMap<string, SelectableRow>,
  targetId: string,
  scope: SelectionScope,
): SelectionState {
  const scoped = selectionForScope(s, scope);
  const targetRow = rowsById.get(targetId);
  if (!targetRow) return scoped;
  if (!scoped.anchorId || !scoped.anchorBase) {
    const next = setRowChecked(scoped, targetRow, true);
    return {
      ...next,
      scope,
      anchorId: targetRow.id,
      anchorBase: { included: new Map(next.included), excluded: new Map(next.excluded) },
    };
  }

  const fromIdx = visibleIds.indexOf(scoped.anchorId);
  const toIdx = visibleIds.indexOf(targetId);
  if (fromIdx === -1 || toIdx === -1) return scoped;
  const [start, end] = fromIdx < toIdx ? [fromIdx, toIdx] : [toIdx, fromIdx];
  const range = visibleIds.slice(start, end + 1);

  let next: SelectionState = {
    ...scoped,
    included: new Map(scoped.anchorBase.included),
    excluded: new Map(scoped.anchorBase.excluded),
  };
  for (const id of range) {
    const row = rowsById.get(id);
    if (row) next = setRowChecked(next, row, true);
  }
  return { ...next, scope, anchorId: scoped.anchorId, anchorBase: scoped.anchorBase };
}
