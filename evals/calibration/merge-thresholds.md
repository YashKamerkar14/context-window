# Merge and suppression thresholds

Calibrated 2026-10-08 for `openai/text-embedding-3-small` (1536 dimensions) with `cw calibrate thresholds`.

## Data

`evals/datasets/story_pairs.jsonl`: 100 pairs built by `cw calibrate pairs` from the stories of a real run.

- 50 same-event pairs: a real story and the version another outlet would write (the fast model, told to use different words and lead with a different aspect).
- 50 different-event pairs: the most similar pairs of different real stories. Release pairs are excluded: releases never merge by embedding.

Labels come from how each pair was made, not from hand labelling. The different-event pairs nearest the merge threshold were read and confirmed to be different events.

## Cosine similarity

| Pairs | Min / median / max |
|---|---|
| Same event | 0.816 / 0.899 / 0.963 |
| Different event | 0.444 / 0.476 / 0.665 |

## Chosen thresholds

| Setting | Value | Why |
|---|---|---|
| `MERGE_THRESHOLD` | 0.81 | Merged without asking: 100% of same-event pairs, precision 100% |
| `MERGE_ASK_THRESHOLD` | 0.70 | 0.03 above the most similar different-event pair; 100% of same-event pairs are at or above it. The 0 pairs in between go to the judge |
| `SUPPRESS_THRESHOLD` | 0.84 | 0.03 above merging: a false suppression would hide real news |

## The same-event judge

On the 20 hardest pairs (the least similar same-event pairs and the most similar different-event pairs), the fast model (`anthropic/claude-haiku-5.5`) answered 20 correctly.

## Re-calibrate when

- the embedding model or its dimensions change;
- merges look wrong in practice (add the bad pairs to the set and run again).
