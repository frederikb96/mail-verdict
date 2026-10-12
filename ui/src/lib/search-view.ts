/**
 * What the search page shows while a text search and a semantic search for
 * the same query are both in flight.
 *
 * In semantic mode the text result renders at once (muted, with a note that
 * the meaning search is still running) and is replaced by the tiered list
 * when that lands; if the server answers 503 the text list stays, with a note
 * that semantic search is unavailable. A response that belongs to an earlier
 * query or window than the one now on screen is ignored.
 */

/** The HTTP status the server answers when the query could not be embedded. */
export const SEMANTIC_UNAVAILABLE_STATUS = 503;

export interface Fetched<T> {
  /** The query this response answers: text, scope and window. */
  key: string;
  status: "pending" | "success" | "error";
  items?: T[];
  errorStatus?: number;
  errorMessage?: string;
}

export interface SearchView<T> {
  items: T[];
  /** Which response `items` came from. */
  source: "text" | "semantic" | "none";
  /** Showing text results as a stand-in while the meaning search runs. */
  muted: boolean;
  notice: "searching" | "unavailable" | null;
  /** Nothing to show yet. */
  loading: boolean;
  error: string | null;
}

function nothing<T>(): SearchView<T> {
  return { items: [], source: "none", muted: false, notice: null, loading: false, error: null };
}

export function chooseSearchView<T>(args: {
  semantic: boolean;
  /** The key of the query on screen now. */
  currentKey: string;
  text: Fetched<T>;
  meaning: Fetched<T>;
}): SearchView<T> {
  const { semantic, currentKey } = args;
  const text = args.text.key === currentKey ? args.text : { ...args.text, status: "pending" as const };
  const meaning =
    args.meaning.key === currentKey ? args.meaning : { ...args.meaning, status: "pending" as const };

  const textView = (notice: SearchView<T>["notice"], muted: boolean): SearchView<T> => {
    if (text.status === "success") {
      return { ...nothing<T>(), items: text.items ?? [], source: "text", muted, notice };
    }
    if (text.status === "error") {
      return { ...nothing<T>(), notice, error: text.errorMessage ?? "Search failed." };
    }
    return { ...nothing<T>(), notice, loading: true };
  };

  if (!semantic) return textView(null, false);

  if (meaning.status === "success") {
    return { ...nothing<T>(), items: meaning.items ?? [], source: "semantic" };
  }
  if (meaning.status === "error") {
    if (meaning.errorStatus === SEMANTIC_UNAVAILABLE_STATUS) return textView("unavailable", false);
    return { ...nothing<T>(), error: meaning.errorMessage ?? "Search failed." };
  }
  return textView("searching", true);
}
