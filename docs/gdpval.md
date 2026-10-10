# GDPval public data and grading limitation

> Retired integration. GDPval has been removed from the active catalog and
> package at the user's request. The text and validation links below are a
> historical record; these commands and source paths are no longer supported.

GDPval is registered as `gdpval@gold-11e7900-v1` with all 220 publicly released gold tasks. Its prompts, reference files, rubrics, and human deliverables are pinned to [official dataset revision `11e7900`](https://huggingface.co/datasets/openai/gdpval/tree/11e7900cdcac61bc4daf59e65feb238acda98fbf).

**Native automatic grading is blocked.** The [official grading page](https://evals.openai.com/gdpval/grading) now directs researchers to build their own workflows using the released rubrics and deliverables. It identifies pairwise human expert preferences as the grading standard and describes model judges as rough estimates. The [OpenAI-hosted leaderboard is no longer active](https://evals.openai.com/gdpval/leaderboard). This limitation is an upstream service change, not a missing API key.

`list` and `preview` expose the complete public task manifest. `check` reports `gdpval_native_grading` as unavailable. `evaluate()` stops before downloading task assets or calling the target model; it does not return a fabricated score. Supplying a subscription or API key cannot restore the retired grader. A new model judge would require a separately named protocol and would not reproduce the original expert preference metric.

The paper's 1,100 other tasks are not public. This profile covers the entire 220-task public release across 44 occupations and nine sectors, not the full private 1,320-task collection.

The example `examples/benchmarks/gdpval.json` selects one public task. Preview it without downloading data:

```bash
rsi-eval preview --config examples/benchmarks/gdpval.json
```

Running this evaluation reports the native grading blocker before any model call. Use the export command below to prepare work for human review.

## Export the original inputs for human evaluation

The SDK includes a preparation helper that exports selected or complete public tasks without scoring them. Reading the Parquet source requires `pyarrow`; the SDK core retains no mandatory dependencies. A separate Python environment can provide the reader:

```bash
python3.12 -m venv .gdpval-data
.gdpval-data/bin/python -m pip install pyarrow==23.0.1

# All 220 prompts and original metadata, with no large file downloads.
python -m rsi_eval.materializers.gdpval \
  --output runs/gdpval-metadata \
  --dataset-python .gdpval-data/bin/python \
  --metadata-only --include-gold

# One complete public task, including its reference and human deliverable files.
python -m rsi_eval.materializers.gdpval \
  --output runs/gdpval-one \
  --dataset-python .gdpval-data/bin/python \
  --task 83d10b06-26d1-4636-a32c-23f92c57f30b \
  --include-gold
```

Repeat `--task` to select more IDs. Omit it to select all 220. Omit `--metadata-only` to download the original files. The complete release references 261 input files totaling about 1.66 GB and 248 gold deliverables totaling about 623 MB. Files are fetched only for selected tasks. All downloads use the immutable revision and verify their SHA256 or original Git blob identity and byte length.

Each public task directory contains the unchanged `prompt.md`, a small `task.json`, and its `reference_files/` tree. `--include-gold` places original rubrics and deliverables in a separate `private_gold/` directory for human reviewers. The root `.sources/` directory contains the source Parquet, which also includes private answers. Give an agent only its public task directory; keep `.sources/` and `private_gold/` outside its context.

`export.json` records selection, source revision, downloaded asset hashes, and the fact that no scoring or model call occurred. Exported files can support an external human review workflow. The export command is not a successful `evaluate()` run.

## Validation and decision boundary

The [validation report](validation-gdpval.json) checks all 220 source records, unique task identities, occupation and sector coverage, rubrics, and every referenced file against the pinned upstream manifest. A full metadata export completed. One task's actual reference and gold deliverable downloads were checked against their source identities. The remaining large files were not all downloaded, and no model run or human grading was performed.

The benchmark remains visible with `grading-blocked` status so the project owner can decide whether to keep it for human evaluation, request a separate approximate judging protocol, or remove it. The implementation does not silently substitute another benchmark or score.
