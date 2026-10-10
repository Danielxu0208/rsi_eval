# JobBench

`jobbench@d43a665-660b33a-v1` contains every released task in the pinned public
source: **65 main tasks and 63 easy tasks**. Split labels remain metadata.
Choose either split, a custom selection, or all 128 records for your own RSI loop.
The official leaderboard score uses the 65 main tasks alone.

The adapter uses the [official Harbor renderer](https://github.com/Job-Bench/job-bench-eval/tree/660b33a86ed032559df0b2fdc1ad38cf7ceb83f3/harbor)
and the original judge. It pins evaluator commit
`660b33a86ed032559df0b2fdc1ad38cf7ceb83f3` and
[dataset revision `d43a66575635da0d3f6cb9a20976dc1a60f3ffc9`](https://huggingface.co/datasets/JobBench/job-bench/tree/d43a66575635da0d3f6cb9a20976dc1a60f3ffc9).
The catalog records SHA256 digests and sizes for all **878 raw files**, totaling
279,496,802 bytes. Each task downloads only its own inputs. Runtime preparation
also verifies all 44 files in the pinned evaluator archive.

## Run a task

Preview is offline and calls no model:

```sh
rsi-eval preview --config examples/benchmarks/jobbench.json
```

Codex subscription login can run the target agent. JobBench uses a **separate
paid API judge**, `grok-4.3` at `https://api.x.ai/v1`. Codex authentication does not
provide that credential. Set `JUDGE_API_KEY` through your private environment,
then run:

```sh
rsi-eval check --config examples/benchmarks/jobbench.json
rsi-eval run --config examples/benchmarks/jobbench.json
```

Alternatively, add a `benchmark_runtime` object under `options`:

```json
{
  "judge_env_file": "/absolute/private/path/jobbench.env"
}
```

The file contains one `JUDGE_API_KEY=...` assignment. It remains outside the
repository and experiment artifacts. The runner passes the value to the Harbor
process in memory; the task sends it only to its separate verifier container.
Only the file path appears in run configuration. A missing credential fails
preflight before candidate evaluation, without emitting a score.

Docker must support Harbor's separate verifier environments and artifact
collection. The installed Harbor 0.18.0 runtime accepts this native configuration.
The upstream guide provisions 0.22.0; the adapter does not infer capabilities
from that version number. The agent environment has public network access, as
in the original benchmark. Its chosen agent must provide any needed search,
browser, or vision tools. Live sites and external search services can require
additional access and can change over time.

## Select the full main split

Omitting `task_ids` selects all 128 released records. To select the complete
65-task leaderboard split, create a manifest from the catalog:

```sh
python - <<'PY'
import json
from pathlib import Path
from rsi_eval.benchmarks import list_benchmarks

spec = next(s for s in list_benchmarks()
            if s["name"] == "jobbench" and s["version"] == "d43a665-660b33a-v1")
task_ids = [t["id"] for t in spec["tasks"] if t["split"] == "main"]
Path("jobbench-main.json").write_text(json.dumps(task_ids, indent=2) + "\n")
PY
```

Replace the example's `task_ids` with `"manifest": "../../jobbench-main.json"`.
Paths resolve relative to the configuration file. Replace `main` with `easy`
for all 63 simplified tasks. Your method chooses the evolve and test sets; the
framework assigns neither role.

## Preserved protocol

The native Harbor instructions, task inputs, rubric files, judge, document
extractors, verifier script, and Dockerfiles remain unchanged. Agents read
`/workspace/task_folder` and submit files in `/workspace/output`. Task cards,
rubrics, and hidden search references are withheld. All 57 tasks whose source
lists search references remain included. Those references are not supplied to
the agent, matching upstream preparation.

Harbor collects the submitted output directory and restores it in the native
separate verifier environment. The judge reads saved notebook output and PPTX
tables and uses the original pinned LibreOffice runtime for supported workbook
formulas. Generated `provenance.json` records input hashes, source revisions,
judge identity, and explicit runtime options. `upstream-task.toml` preserves the
unmodified renderer configuration for comparison.

Each rubric block earns its full weight only if every criterion passes. The
native task reward is `round(earned_weight / possible_weight, 4)`. The summary
reports the equal-task mean, its percentage scale, and separate split summaries.
It does not pool raw rubric weights across tasks. A complete official main
result requires all 65 tasks and one valid judgment per task. Mixed splits,
subsets, repeated trials, and missing judgments must be described as such.

Harbor retains `judge-details.json`, rubric response logs, extraction records,
`verification.json`, and `reward.json`. API, parsing, extraction, and unsafe-file
failures produce failed verification without a valid reward. Empty submissions
receive the original zero score without a judge call. That path is a negative
check, not an oracle solution. No gold deliverables are released, so the adapter
rejects `entrypoint: "oracle"` for this benchmark.

Judge usage and dollar cost remain unknown because the original evaluator does
not report them. They are separate from target-agent tokens and must not be
recorded as zero or included in the target's optimization cost.

## Runtime options

Defaults match the pinned native route:

| Option | Default | Meaning |
| --- | --- | --- |
| `judge_env_file` | Unset | Private file containing `JUDGE_API_KEY` |
| `judge_model` | `grok-4.3` | Judge model; changing it defines a different scoring configuration |
| `judge_api_base` | `https://api.x.ai/v1` | HTTPS judge endpoint |
| `judge_max_workers` | 10 | Parallel rubric judgments within a task |
| `judge_max_retries` | 3 | Native retry limit |
| `judge_timeout_per_rubric` | 300 | Native rubric timeout in seconds |
| `agent_timeout_sec` | 7200 | Target-agent time limit |
| `verifier_timeout_sec` | 3600 | Native verifier time limit |
| `build_timeout_sec` | 1800 | Agent and verifier image build limit |

## Resume an interrupted evaluation

Restore the required credentials or services, then resume in the original output
directory. The example configuration writes to `runs/jobbench-example`:

```sh
rsi-eval resume --output runs/jobbench-example
```

Recovery validates the saved candidate, benchmark, runtime settings and evaluator
identity. It retains completed measurements, including valid zero scores, and
retries only unfinished task repetitions. Each explicit invocation makes at most
one new attempt per missing repetition. Prior artifacts and known usage remain
recorded; a retry can make new target and judge calls.

Use a new output directory when changing the candidate, selection or scoring
configuration. Older runs without the required execution identity cannot use
this recovery path. Preserve those records and start a separate evaluation of
the unfinished task IDs. See [the shared recovery guide](methods.md#recover-work)
for SDK calls and managed method experiments.

## Validation status

The complete input inventory was downloaded and verified against pinned Hugging
Face blob metadata and the catalog. All 128 packages were generated and their
native rubrics exercised with empty submissions. Fifteen offline regression
checks cover source identity, private rubric separation, weighted scoring, task
configuration, credential handling, and invalid-judgment rejection.

Both original images built successfully on Mac with Linux ARM Docker. A
network-disabled verifier run validated text, saved notebook output, PPTX tables,
SQLite records, and actual LibreOffice formula recalculation. The agent sandbox
passed input hash checks, output permission checks, 28 Python module imports,
and native Office/PDF/OCR/GDAL tool probes. It created DOCX, PPTX, XLSX, SQLite,
and PDF deliverables and passed a LibreOffice DOCX-to-PDF conversion followed by
text extraction. These checks made no model or judge calls. An SDK call without
judge credentials retained all 128 selected tasks as infrastructure failures
and reported no score.

The remaining scoring blocker is **missing judge API access**. No nonempty
submission has been scored with the real `grok-4.3` judge. This integration does
not claim a complete scored run, a solved-task oracle, or paper reproduction.
See [the machine-readable validation report](validation-jobbench.json) for exact
checks and the separate native verifier image validation status.
