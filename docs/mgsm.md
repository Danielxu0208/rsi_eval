# MGSM agent evaluation

`mgsm@1.0-agent` contains all 250 questions in each of 11 languages from
[MGSM](https://github.com/google-research/url-nlp/tree/452a21ad3dae5668c06ceeac21ff073e1e40f9be/mgsm).
The catalog pins the repository commit and the SHA256 of every TSV file.
It contains 2,750 task IDs, from `bn-001` through `zh-250`. Numbering follows
the one-based row order within each language. The same problem appears in
multiple translations, so different languages do not establish disjoint data.

## Submission and scoring

The target receives the original question and writes a numeric answer to
`/app/answer.txt`. It can use its normal tools. The adapter supplies no solved
demonstrations or default train/test split. This is a zero-shot, tool-assisted
agent protocol. Configure labeled context or a shuffled development selection
in the calling method when your experiment needs them.

The verifier trims file whitespace, removes decimal trailing zeros and commas,
then compares the resulting numeric string with the gold answer. The comparison
follows [ADAS's MGSM scorer](https://github.com/ShengranHu/ADAS/blob/2702bee8fefda42255efc5be9f60e3bd3db96ae4/_mgsm/utils.py).
For example, `1,000.00` matches `1000`; an explanation containing `1000` does
not. A missing or malformed answer receives zero. A failed download or verifier
does not become a measured wrong answer.

Each task gets a separate container and one trial per requested repetition.
The default task budget is 180 seconds for the agent and 30 seconds for grading.
Answers and the verifier enter the container after the agent finishes. An oracle
copies the dataset answer to the submission path to check the execution and
grading chain; it is not an independently derived mathematical solution.

`result.metrics.benchmark_metrics` reports overall accuracy and accuracy for
each selected language. These aggregates remain unknown when a trial is missing
or has an infrastructure error. Raw rewards and predictions remain in the native
verifier artifacts. Subscription token usage comes from the agent, not grading.

## Run a small selection

After the main README's Harbor and Codex setup, edit the model and dedicated
authentication path in `examples/benchmarks/mgsm.json` if needed:

```sh
.venv/bin/rsi-eval preview --config examples/benchmarks/mgsm.json
.venv/bin/rsi-eval run --config examples/benchmarks/mgsm.json \
  --oracle --output runs/mgsm-oracle
.venv/bin/rsi-eval run --config examples/benchmarks/mgsm.json \
  --output runs/mgsm-candidate
```

The example selects `en-001` and `zh-002`. Replace `task_ids`, use a JSON task
manifest, or omit the selection to run all 2,750 entries. Choose evolve and test
sets in the calling method. Nothing in the catalog fixes those roles.

Preparation downloads only the selected languages and the upstream CC BY 4.0
license. It stores source files, hashes, generated instructions, and hidden
verifier inputs under the run directory. Preview never downloads data. The
core remains dependency-free; this adapter's container uses Python.
