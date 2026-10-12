# Reasoning and MCE benchmarks

These integrations expose every record in the pinned public releases through `evaluate()`. Selection remains the caller's choice. Each task becomes an isolated Harbor trial, with the question available to the agent and the reference answer uploaded to the verifier after solving. No model judge or API key is required.

| Name | Version | Public records |
| --- | --- | ---: |
| `mmlu` | `original-all-splits-v1` | 115,700 |
| `gpqa` | `main-56686c0-v1` | 448 |
| `gpqa` | `diamond-56686c0-v1` | 198 |
| `gpqa` | `extended-56686c0-v1` | 546 |
| `arc-agi` | `1-public-3990304-v1` | 800 |
| `mce-finer` | `meta-harness-8123cca-v1` | 400 |
| `mce-uspto` | `meta-harness-8123cca-v1` | 180 |
| `mce-symptom2disease` | `meta-harness-8123cca-v1` | 462 |
| `mce-lawbench` | `meta-harness-8123cca-v1` | 350 |
| `mce-aegis` | `meta-harness-8123cca-v1` | 668 |

The GPQA configurations overlap. Their counts do not represent distinct questions. MCE names identify the published Meta-Harness configurations, not every record in the larger datasets from which they were drawn.

## Run a small example

The source archive includes one-task configurations for all ten profiles in `examples/benchmarks/`: `mmlu.json`, `gpqa-main.json`, `gpqa-diamond.json`, `gpqa-extended.json`, `arc-agi.json`, and one `mce-*.json` per configuration. These examples use the editable Codex target and the same subscription setup as the main README.

```bash
# Offline preview: no model, Docker, source download, or optional dependency.
rsi-eval preview --config examples/benchmarks/mmlu.json

# Exercise the private verifier with the dataset answer; no model call.
rsi-eval run --config examples/benchmarks/mmlu.json --oracle --output runs/mmlu-oracle

# Evaluate the configured target using its dedicated Codex login.
rsi-eval run --config examples/benchmarks/mmlu.json --output runs/mmlu-candidate
```

Replace the config filename to use another profile. The examples select one task explicitly. Remove `task_ids` to select the entire pinned configuration.

## Select a split without assigning an experiment role

Omitting `task_ids` selects the complete pinned profile. To choose a split, generate a manifest from the public catalog:

```python
import json
from pathlib import Path
from rsi_eval import BenchmarkSelection
from rsi_eval.benchmarks import resolve

spec = resolve(BenchmarkSelection("mmlu", "original-all-splits-v1"))
task_ids = [task["id"] for task in spec["tasks"] if task["split"] == "test"]
Path("mmlu-test.json").write_text(json.dumps({"task_ids": task_ids}))
selection = BenchmarkSelection(
    "mmlu", "original-all-splits-v1", manifest=Path("mmlu-test.json")
)
```

Pass this selection to `evaluate()` in your own search or evaluation loop. Grouped summaries retain split and subject boundaries. An average over all selected splits is not the official test-set score.

## MMLU

The source is the complete [original author-hosted archive](https://people.eecs.berkeley.edu/~hendrycks/data.tar), pinned by SHA256. The reference implementation is fixed at [`4450500`](https://github.com/hendrycks/test/tree/4450500f923c49f1fb1dd3d99108a0bd9717b660).

| Original split | Records | Task ID pattern |
| --- | ---: | --- |
| `auxiliary_train` | 99,842 | `auxiliary_train-race-00001` |
| `dev` | 285 | `dev-abstract_algebra-00001` |
| `val` | 1,531 | `val-abstract_algebra-00001` |
| `test` | 14,042 | `test-abstract_algebra-00001` |

Test and validation retain the original five development examples for the same subject. Exposed development and auxiliary-training records receive no labeled context, which prevents an item's own answer from appearing in its prompt. `benchmark_runtime.few_shot` accepts integers from zero through five and changes the context size for test and validation only.

The agent writes one uppercase letter to `/app/answer.txt`. Scoring preserves exact choice accuracy. This is a declared agent submission protocol: the original script selects the highest-probability answer token using model log probabilities, while this integration grades the agent's submitted letter. The complete archive includes auxiliary data drawn from other multiple-choice corpora; only the 14,042-record test split is the original MMLU test set.

## GPQA

All three question files come from the [official GitHub archive](https://github.com/idavidrein/gpqa/tree/56686c06f5e19865c153de0fdb11be3890014df7). The authors publish the ZIP password in their README. This source is available without Hugging Face authentication. The archive also contains expert metadata; those rows are not question tasks.

The loader matches the original choice ordering procedure: start with three incorrect answers and the correct answer, then shuffle using Python's seeded random generator. Seed zero is the default. Shuffling always processes the entire configuration before task selection, so selecting a subset cannot change a question's options. Set `benchmark_runtime.choice_seed` to an integer to use another recorded ordering.

The agent submits `A`, `B`, `C`, or `D` to `/app/answer.txt`. The original `Question` and answer fields are used, as in the reference loader. Private references retain source `Record ID`; explanations and expert annotations are excluded from the agent prompt. Choice accuracy remains binary. Agent tool use is part of the submitted target and should be reported when comparing against closed-book or retrieval baselines.

## ARC-AGI-1

This is [Chollet's original public corpus](https://github.com/fchollet/ARC-AGI/tree/399030444e0ab0cc8b4e199870fb20b863846f34), with 400 training and 400 evaluation tasks. It does not include hidden competition tasks or ARC-AGI-2/3.

The original rule permits three attempts per test input. A task succeeds only when every test input has an exactly matching output among its attempts, including grid dimensions. The public prompt contains all training pairs and only the input grids of test pairs.

Write `/app/answer.json` in this format:

```json
{"predictions": [[[[1, 2], [3, 4]], [[1, 2], [3, 0]]]]}
```

The example contains one test input with two grid attempts. Supply one list of one to three grids for every test input, in order. Grids must be rectangular, at most 30 by 30, and contain integer colors from zero through nine. Boolean values, ragged arrays, extra attempts, and missing test inputs fail validation. This adapter uses the original three-attempt rule, not the two-attempt rule used in some later competitions.

## Meta-Harness MCE configurations

The source is the [published Meta-Harness text-classification data](https://github.com/stanford-iris-lab/meta-harness/tree/8123ccabe2b19fa1123090b2e5f1bacc20963ce8/reference_examples/text_classification/data). It includes five configurations copied from the MCE artifact:

| Configuration | Train | Validation | Test | Native metrics |
| --- | ---: | ---: | ---: | --- |
| FiNER | 200 | 100 | 100 | Exact normalized answer |
| USPTO | 50 | 30 | 100 | Reactant-set exact match and Jaccard |
| Symptom2Disease | 200 | 50 | 212 | Normalized diagnosis match |
| LawBench crime prediction | 200 | 50 | 100 | Charge-set exact match, F1, precision, recall and counts |
| AEGIS | 400 | 128 | 140 | Original safe/unsafe parser and exact match |

Task IDs use `train-0001`, `val-0001`, and `test-0001`. The original AEGIS filename `valid.jsonl` maps to the catalog split `val`; its bytes and row order remain unchanged.

The adapter executes the original MCE loader function bodies to form prompts and copies the original evaluator and constants files into the private verifier. Downloaded files are checksummed. The optional Hugging Face loaders for unrelated out-of-distribution datasets are not imported. The agent writes its complete response to `/app/answer.txt`; the upstream parser handles JSON, diagnosis markers, charge markers, and other supported formats.

The primary reward is the original binary `was_correct` result. Native partial metrics remain in `reward.json` and `grading.json`. USPTO uses the upstream string-set comparison, without introducing a different chemical-equivalence scorer. LawBench covers the published crime-prediction configuration, not the entire LawBench suite.

These are per-record benchmark interfaces. The integration does not recreate the text-classification experiment's memory training loop, automatically expose gold labels to candidates, or share mutable memory between isolated trials. Researchers can load the pinned training data in their own method and place learned memory in the candidate artifact before evaluating held-out records. Existing Meta-Harness/RRSI method launchers retain their documented selection semantics.

## Runtime and provenance

The task image is `python:3.12.12-slim-bookworm` and requires no additional Python packages. The task timeout defaults to 300 seconds and can be changed with `benchmark_runtime.agent_timeout`. MMLU's first source download is approximately 166 MB; source data is not bundled in the SDK. Set `RSI_EVAL_SOURCE_CACHE` to reuse already verified bytes.

Each generated task records the source hash, original record identity, split, protocol, and configured options. Gold files remain under `tests/`, outside the candidate image. Ordinary Harbor reward files and trajectories remain available alongside the summary.
