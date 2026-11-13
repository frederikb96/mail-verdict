/**
 * Reordering a rule list that is shown in groups. The server keeps one
 * order for the whole document; a group is a view of some of its slots, so
 * a move inside a group permutes only that group's slots and leaves every
 * other rule exactly where it was.
 */

/** `order` with the slots held by `groupIds` refilled, in order, by `newGroupOrder`. */
export function reorderWithinGroup(
  order: string[],
  groupIds: string[],
  newGroupOrder: string[],
): string[] {
  const members = new Set(groupIds);
  const slots = order.flatMap((id, i) => (members.has(id) ? [i] : []));
  const next = [...order];
  slots.forEach((slot, i) => {
    next[slot] = newGroupOrder[i];
  });
  return next;
}

/** `order` after moving `id` one place up or down among the group's members. */
export function moveWithinGroup(
  order: string[],
  groupIds: string[],
  id: string,
  direction: -1 | 1,
): string[] {
  const inOrder = order.filter((x) => groupIds.includes(x));
  const from = inOrder.indexOf(id);
  const to = from + direction;
  if (from === -1 || to < 0 || to >= inOrder.length) return order;
  const moved = [...inOrder];
  [moved[from], moved[to]] = [moved[to], moved[from]];
  return reorderWithinGroup(order, inOrder, moved);
}
