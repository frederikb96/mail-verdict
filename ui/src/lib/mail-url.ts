/**
 * The mail view's own address: `/?account=<id|unified>&folder=<id>&message=<id>`.
 * Query parameters, not path segments -- next.config.ts's `output: "export"`
 * makes every route a prebuilt HTML file, so a path Next never generated
 * (`/mail/<id>`) would serve the wrong page's markup and hydrate against
 * it. One function builds this URL so every writer (the mail view's own
 * URL-sync effect, a search result being opened) produces the identical
 * shape -- see use-mail-url-sync.ts for why there is exactly one writer.
 */

export interface MailUrlState {
  accountId: string | null;
  isUnified: boolean;
  unifiedFolder: string | null;
  folderId: string | null;
  messageId: string | null;
}

export function buildMailUrl(state: MailUrlState): string {
  const params = new URLSearchParams();
  if (state.isUnified) {
    params.set("account", "unified");
    if (state.unifiedFolder) params.set("folder", state.unifiedFolder);
  } else if (state.accountId) {
    params.set("account", state.accountId);
    if (state.folderId) params.set("folder", state.folderId);
  }
  if (state.messageId) params.set("message", state.messageId);
  const query = params.toString();
  return query ? `/?${query}` : "/";
}
