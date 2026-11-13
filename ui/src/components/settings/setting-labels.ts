/** A hand-authored label for a settings field whose raw key doesn't
 * already read as a sentence with its unit attached -- "notify_wait_
 * seconds" as "Wait before notifying (seconds)" rather than "Notify wait
 * seconds". Several categories share a field name with the same meaning
 * (base_delay_seconds, max_attempts, provider, model, ...), so this is
 * one map for all of them rather than one per category. Anything not
 * listed falls back to humanizeSettingKey() below, so a newly added
 * setting still renders as something readable rather than nothing. */
export const SETTING_LABELS: Record<string, string> = {
  provider: "Provider",
  model: "Model",
  base_url: "Server address (custom provider only)",
  reasoning_effort: "Reasoning effort",
  max_tokens: "Max tokens",
  max_retries: "Max retries",
  base_delay_seconds: "Base retry delay (seconds)",
  max_delay_seconds: "Max retry delay (seconds)",
  exponential_base: "Retry backoff multiplier",
  lease_seconds: "Worker lease (seconds)",
  poll_interval_seconds: "Poll interval (seconds)",
  max_attempts: "Max attempts",
  unavailable_probe_seconds: "Retry after unavailable (seconds)",
  live_max_age_days: "Ignore mail older than (days)",
  enabled: "Enabled",
  content_chars: "Characters embedded per message",
  batch_size: "Backfill batch size",
  neighbor_k: "Similar past messages consulted",
  neighbor_min_similarity: "Minimum similarity",
  query_instructions: "Search query instruction per embedding model",
  default_event_duration_minutes: "Default event duration (minutes)",
  default_reminder_minutes: "Default reminder (minutes)",
  undo_send_seconds: "Undo window after Send (seconds)",
  mark_read_on_file_to_archive_or_junk: "Mark read when filed to Archive or Junk",
  bell_badge_counts_new_mail: "Bell badge counts new mail",
  notify_wait_seconds: "Wait before notifying (seconds)",
  language: "Language of titles and summaries",
  filter: "First filter (patterns)",
  call_timeout_seconds: "Model call timeout (seconds)",
  decide_provider: "Order matching: provider",
  decide_base_url: "Order matching: server address (custom provider only)",
  decide_model: "Order matching: model",
  decide_reasoning_effort: "Order matching: reasoning effort",
  decide_max_tokens: "Order matching: max tokens",
  write_provider: "Order text: provider",
  write_base_url: "Order text: server address (custom provider only)",
  write_model: "Order text: model",
  write_reasoning_effort: "Order text: reasoning effort",
  write_max_tokens: "Order text: max tokens",
  read_provider: "Image reading: provider",
  read_base_url: "Image reading: server address (custom provider only)",
  read_model: "Image reading: model (takes images; empty = OCR only)",
  read_reasoning_effort: "Image reading: reasoning effort",
  read_max_tokens: "Image reading: max tokens",
  read_below_confidence: "Image reading: when OCR confidence is below (0-100)",
};

/** A raw settings key read as a sentence rather than the key itself --
 * SETTING_LABELS above wins when it names the key; this is the fallback
 * for anything it doesn't. Display only, in the generic renderer that
 * draws whatever fields a category's GET happens to return; onChange
 * still keys by the real name. The raw key stays reachable as the
 * label's title attribute, for cross-referencing against the API/config
 * docs, which use it. */
export function humanizeSettingKey(key: string): string {
  if (SETTING_LABELS[key]) return SETTING_LABELS[key];
  const [first, ...rest] = key.split("_");
  return [first.charAt(0).toUpperCase() + first.slice(1), ...rest].join(" ");
}

