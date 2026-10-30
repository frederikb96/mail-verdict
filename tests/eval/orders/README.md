# Orders evaluation

A manual harness that measures the two model calls of the orders pipeline, decide (which entry a mail
belongs to, a new one, or none) and write (title, status, summary, kind, dates, key facts), against
known answers. It calls real models, so it is never part of CI; CI scores recorded answers offline
(`tests/unit/test_eval_scorer.py`). The measured result and the model choice it supports are in
[RESULTS.md](RESULTS.md).

## Layout

- `cases/*.json` — frozen inputs, one call each. Synthetic: no real mail is committed.
- `expectations.yaml` — what each case must produce; the keys are described in `scorer.py`.
- `scorer.py` — hard failures (a wrong verdict, kind, date, icon, a missing fact, an identifier or code
  reference the input does not contain, a credential copied out of the input) and soft ones (a format
  the prompt asks for).
- `recorded/` — answers of earlier runs, scored again by the unit test.
- `../../../scripts/eval_orders.py` — `build`, `run`, `rescore`, `judge`, `table`, `tally`.

The system prompts and the assembly of the user prompt are the product's own (`orders/prompts.py`,
`orders/documents.py`), read on every run, so a run measures the prompt files as they stand.

## Running

```bash
# from the repository root; the keys are read through the `secret` CLI
uv run --extra dev -- python scripts/eval_orders.py run --out "$OUT" --label q38-low \
  --model infomaniak:Qwen/Qwen3.8-27B-FP8 --effort low --base-url "$INFOMANIAK_BASE_URL" --jobs 3
uv run --extra dev -- python scripts/eval_orders.py run --out "$OUT" --label opus-low \
  --model anthropic:claude-opus-5-5 --effort low --tasks write --jobs 3
uv run --extra dev -- python scripts/eval_orders.py table --out "$OUT" q38-low opus-low
```

- One record per case and run under `$OUT/<label>/`; a rerun skips what is on disk, so an interrupted
  run resumes.
- At most three concurrent calls reach Infomaniak (more are rate limited). Every call has the timeout
  of `orders.call_timeout_seconds`; a timeout is a failed attempt.
- `run` makes up to two calls per case without feeding errors back and records whether the first was
  valid; the retry would otherwise hide a model that always needs one.
- `tally` sums tokens and cost from the records; `PRICES` in the script holds the per-million prices.

## Cases from a real mailbox

`build --from <export> --manifest <file> --cases-dir <dir>` makes cases the same way the pipeline reads
mail: the prepared body, every attachment through the extractor, candidates built from the stored
entries. The export is a directory with `orders/`, `msgs/` and `att/`. Real cases hold real mail: keep
them and their expectations outside the repository and pass `--cases-dir` and `--expect` to `run`. The
harness refuses to send a decide case from such a directory to an external provider; the decide call
reads ordinary mail and stays on Infomaniak.

## Changing a prompt

1. Edit `config/prompts/orders_*.md.j2`.
2. Rerun the failing cases on one fast model into a new label; compare the failing set with the
   previous label.
3. When they pass, run the whole set once on that model, then the comparison across models.
4. `tests/unit/test_order_system_prompts.py` pins the rules a measurement added; a reworded prompt
   that drops one fails it.
