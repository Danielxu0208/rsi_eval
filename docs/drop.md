# DROP agent evaluation

`drop@1.0-agent` exposes all 86,945 records in the official public release:
77,409 from train and 9,536 from dev. The catalog retains the original split
labels as provenance. Users choose which records their method uses to evolve
and which it uses to test. The hidden test set is not included.

The adapter pins the [official DROP archive](https://s3-us-west-2.amazonaws.com/allennlp/datasets/drop/drop_dataset.zip)
by SHA256 and downloads its 8.3 MB payload during preparation. It also checks
the extracted JSON files and data license. The evaluator comes unchanged from
[AllenAI's official implementation](https://github.com/allenai/allennlp-models/blob/b1f372248c17ad12684d344955fbcd98e957e77e/allennlp_models/rc/tools/drop.py).
The source commit and every downloaded file hash appear in the catalog.

## Submission and metrics

The agent receives the original passage and question, then writes its answer to
`/app/answer.json`. A submission is a JSON string or an array of strings for
multiple answer spans:

```json
["Alice", "Bob"]
```

A numeric answer is a string such as `"42"`. A date uses one string in day,
month, year order, omitting unknown components. The adapter adds these file
instructions but supplies no demonstrations. The target may use its normal
tools. This is a declared zero-shot agent protocol, not a reproduction of
ADAS's search split or prompting setup.

The untouched official scorer handles number normalization, dates, multiple
spans, optimal span alignment, and alternative validated answers. It rounds
F1 to two decimals for each question before aggregation. It reports:

| Field | Meaning | Scale |
|---|---|---|
| `exact_match` | Official exact-match score | 0 to 1 |
| `f1` | Official token-overlap F1 with numeric matching constraints | 0 to 1 |
| `reward` | The same F1 value used by the evaluation backend | 0 to 1 |

For example, `"Pacific"` against `"Pacific Ocean"` receives EM 0 and F1 0.67.
The unified result retains this fractional reward. It does not turn it into a
binary pass rate. `result.metrics.benchmark_metrics` contains mean EM and F1,
with separate values for each selected source split. These aggregates remain
unknown when any required trial lacks a valid measured result. Repetitions
contribute equally to the means.

Malformed or missing answer files enter the official missing-prediction branch
and receive zero. A failed download, unavailable scientific dependency, or
verifier crash is an infrastructure failure with no measured reward. The
native artifacts retain `grading.json`, `reward.json`, and verifier output.

## Source details that affect selection

Task IDs normally have the form `dev-<query_id>` or `train-<query_id>`. The dev
release contains two records with query ID
`28553293-d719-441b-8f00-ce3dc6df5398`. Their question text differs in its final
punctuation. The catalog preserves both, using these IDs in source order:

- `dev-28553293-d719-441b-8f00-ce3dc6df5398`
- `dev-28553293-d719-441b-8f00-ce3dc6df5398-occurrence-2`

Each record receives a separate agent submission. The original batch evaluator
keys predictions by query ID and would reuse one prediction for those two
records. This identity distinction is explicit in `protocol.duplicated_query_ids`.

Nine train records have no nonempty reference answer. They remain selectable,
and the official scorer gives them zero. Their IDs appear in
`protocol.empty_reference_task_ids`. The gold-transport oracle rejects selections
containing these records because it cannot provide a positive reference check.
Neither anomaly causes silent filtering or replacement of source data.

Different questions can share a passage. Disjoint question IDs alone do not
establish disjoint passage sets. The generated `provenance.json` records each
source passage ID, query ID, split, archive hash, and grader version.

## Run a small selection

After the README's Harbor and Codex setup:

```sh
.venv/bin/rsi-eval preview --config examples/benchmarks/drop.json
.venv/bin/rsi-eval run --config examples/benchmarks/drop.json \
  --oracle --output runs/drop-oracle
.venv/bin/rsi-eval run --config examples/benchmarks/drop.json \
  --output runs/drop-candidate
```

The example selects these two dev records:

| Task ID | Answer type |
|---|---|
| `dev-f37e81fa-ef7b-4583-b671-762fc433faa9` | Single span |
| `dev-ac6ba235-3024-4f63-a6ab-730a14def4cb` | Number |

The oracle copies a nonempty dataset answer to the submission file to check
transport and grading. It is not an independently derived solution. Hidden
gold annotations and the scorer enter the task container only after the agent
finishes. The Docker build context contains neither answers nor oracle scripts.

Replace `task_ids`, use a JSON manifest, or omit the selection to evaluate every
public record. To construct a manifest from one original split:

```python
import json
from pathlib import Path
from rsi_eval.benchmarks import list_benchmarks

benchmark = next(item for item in list_benchmarks()
                 if item["name"] == "drop" and item["version"] == "1.0-agent")
task_ids = [task["id"] for task in benchmark["tasks"] if task["split"] == "dev"]
Path("drop-dev.json").write_text(json.dumps({"task_ids": task_ids}))
```

Then set `"manifest": "path/to/drop-dev.json"` in the benchmark configuration
and remove `task_ids`. The source split remains a user choice and does not
assign an evolve or test role.

The task budgets are 180 seconds for the agent and 60 seconds for the verifier.
The example sets a 1,800-second whole-job limit for its two records. Increase
or remove that limit when selecting a larger collection. Each task uses its
own Docker container, so a full release run has substantial orchestration and
model costs. Small-subset acceptance does not establish full-dataset results.

`RSI_EVAL_SOURCE_CACHE` enables the existing explicit download cache. Every
cached file is verified against its SHA256 before reuse. Preview performs no
network downloads or model calls. NumPy 2.2.6 and SciPy 1.15.3 run in the task
container; the core SDK still has no runtime dependencies.

## Validation and licensing

Offline tests use the unchanged official evaluator. They cover partial credit,
number constraints, span alignment, dates, alternative gold annotations, invalid
submissions, source integrity, and the duplicate-record mapping. Real execution
status is recorded in the [acceptance report](validation-drop-frontier.md);
successful offline grading alone does not establish a working model or container
path.

The DROP data and sample fixture use CC BY-SA 4.0. The scorer and its fixture
use Apache 2.0. The original license files accompany downloaded sources and
test fixtures. Cite the [DROP paper](https://aclanthology.org/N19-1246/) when
publishing results.
