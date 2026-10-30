# Results

Measured on 2026-10-30 against 62 known-answer cases: 39 write cases and 23 decide cases (including
file placement), of which 25 write and 12 decide cases are built from a real mailbox and 14 write and 11
decide cases are the synthetic ones in `cases/`. Models are called the way the pipeline calls them
(`core/structured_llm.py`, schema-constrained output, no retry feeding errors back), with the timeout
of `orders.call_timeout_seconds` (120 s). Latency is wall time per case; cost is from the recorded
token counts at the providers' list prices.

## Chosen assignment

| Task | Provider | Model | Effort | max_tokens |
|---|---|---|---|---|
| decide | Infomaniak (`custom`) | `Qwen/Qwen3.8-27B-FP8` | `low` | 4000 |
| write | Anthropic | `claude-opus-5-5` | `low` | 8000 |
| rule assistant | Anthropic | `claude-opus-5-5` | `medium` | 8000 |

The assistant row is a choice, not a measurement: its exchange needs a database of mail and a pipeline
document, which this harness does not build.

## Decide

| Model | Effort | Runs | Pass | First answer valid | Latency p50 / max (s) | Cost |
|---|---|---|---|---|---|---|
| Qwen3.8-27B | low | 69 | 69 | 68 | 2 / 122 | 0.13 CHF |
| Qwen3.8-27B | medium | 23 | 23 | 23 | 2 / 4 | 0.05 CHF |
| Qwen3.8-27B | none | 23 | 22 | 21 | 1 / 14 | 0.05 CHF |
| Mistral-Small-4-119B | none | 23 | 19 | 23 | 2 / 3 | 0.01 CHF |

- Qwen3.8-27B passes at `low` and `medium`; `low` is the lowest level that passes every case twice.
  At `none` it left the identifiers empty for a new entry once and twice returned an invalid first answer.
- Mistral-Small-4 chose a wrong target in three cases (it attached a person-to-person payment to a
  parcel entry, attached a parcel mail to a sibling entry of the same shop, and merged a second ticket
  into the first) and left the identifiers of a new entry empty once.
- One `low` call stalled for the whole 120 s timeout on a prompt of a few thousand tokens and answered
  in under 3 s when repeated; another call was rate limited (HTTP 429) beside concurrent traffic.

## Write

| Model | Effort | Runs | Pass | Runs with a format slip | First answer valid | Latency p50 / max (s) | Cost |
|---|---|---|---|---|---|---|---|
| Opus 5.5 | low | 78 | 78 | 13 | 78 | 5 / 9 | 1.0 USD per 39 cases |
| Opus 5.5 | medium | 39 | 39 | 5 | 39 | 5 / 14 | 1.1 USD per 39 cases |
| Qwen3.8-27B | low | 39 | 39 | 14 | 38 | 5 / 123 | 0.20 CHF per 39 cases |
| Haiku 5.5 | medium | 39 | 38 | 16 | 39 | 6 / 13 | 0.04 USD per 39 cases |

Pass is the exact checks only (kind, dates, icon, key facts, codes, credentials, invented identifiers).
On what they cannot check, a model judged the two answers of each case blind and shuffled, listing
concrete errors against the input and scoring 1 to 5:

| Cases judged | Opus 5.5 `medium` better | Equal | Qwen3.8-27B `low` better |
|---|---|---|---|
| 26 (14 synthetic, 12 real) | 20 | 6 | 0 |

Opus scored mostly 3 to 4, Qwen mostly 2 to 3. The errors listed for Qwen that were checked
against the input held: a "five-star review" and an "agreed 15:00 check-out" the mails do not state, a
"monthly subscription renewed" for a single credit purchase, a ticket described as non-transferable
beside a line saying it is transferable. Opus's errors were mostly summary lines repeating the key
facts. Opus `low` and `medium` give the same exact-check results and the same share of format slips.

## What the measurement changed in the prompts

| Failure seen on Qwen3.8-27B `low` | Observed | Edit |
|---|---|---|
| A PIN or temporary password from a mail copied into the summary or facts | every case that held one (4) | write: never write a password, PIN, passcode or access credential |
| Icon unrelated to the entry (a parcel with the car icon, an invoice with the ticket icon) | 14 of 62 runs | write: what each icon is for |
| A carrier's name written as a tracking number | 1 of 2 runs of one case | write: only a number a mail states, never the carrier's name |
| A digital purchase classed as `other` | 2 of 3 runs of one case | write: e-books, audiobooks, apps and games bought for good are orders |
| No identifiers listed for a new entry | 3 runs | decide: list them for a handle and for "new" |

Each edit was followed by the failing cases and then the whole set. Without the icon edit the
whole set passed 49 of 62; with it, 62 of 62.

## Expectations that were wrong

Corrected after a run showed the expectation, not the answer, was wrong about the input or the prompt:
the decide identifiers are required only when the answer is "new" (an answer that attaches to an entry
already holding the number needs none); a document's due date is a date the person must act on, which
the write prompt allows as `event_start` for any kind; a German summary is recognised by a wider word
list and by the absence of English function words; a tracking fact's carrier is checked on its own
field.

## Spend

Claude about 6.5 USD of the 8 USD cap. Infomaniak 1.8 CHF of the 2 CHF cap by the harness's token
tally, 1.9 CHF by the account's own consumption report for the day. Both cover the whole
evaluation including the tuning turns and the judge.
