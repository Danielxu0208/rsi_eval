# LiveCodeBench code generation

`livecodebench@release_v1` adds all 400 tasks from the official `code_generation_lite` release. The bundled catalog contains every task ID, so `list` and `preview` work offline. The adapter covers 219 standard-input problems and 181 function problems. It evaluates one final Python program per trial.

SICA used LiveCodeBench to evaluate self-improving coding agents. This integration retains the public dataset and official answer comparison code. It uses the caller's agent and records its execution budget; the caller owns the search loop. See the [SICA paper](https://arxiv.org/html/2504.15228v2) and [official LiveCodeBench repository](https://github.com/LiveCodeBench/LiveCodeBench).

## Pinned inputs

| Input | Pin |
| --- | --- |
| Dataset | [`livecodebench/code_generation_lite`](https://huggingface.co/datasets/livecodebench/code_generation_lite/tree/0fe84c3912ea0c4d4a78037083943e8f0c4dd505) at `0fe84c3912ea0c4d4a78037083943e8f0c4dd505` |
| Release | `release_v1`, the complete `test.jsonl` file |
| Date filter | May 1, 2023 through March 31, 2024, inclusive; all 400 release records fall within it |
| Dataset SHA256 | `2bd02b38beb48e8c46b5b9987095d999ff38cd8efc255ea5d58974317c48f63f` |
| Official grader | [`testing_util.py`](https://github.com/LiveCodeBench/LiveCodeBench/blob/28fef95ea8c9f7a547c8329f2cd3d32b92c1fa24/lcb_runner/evaluation/testing_util.py) at `28fef95ea8c9f7a547c8329f2cd3d32b92c1fa24` |
| Grader SHA256 | `b7cb6a8a69807bb868150a61742e25d7bb5328bbe01d514471b6ec43c9fa9ed2` |
| Task runtime | Python `3.12.10-slim-bookworm`, manifest `sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db`, NumPy `2.2.6` |

The base image supports Linux amd64 and arm64. Docker builds install the pinned NumPy version and system utilities. The core package and materializer use only the Python standard library. They do not install LiveCodeBench's model-provider, Torch, or vLLM dependencies.

The first preparation downloads **1,252,609,773 bytes**, about 1.25 GB, even for a task subset. Set an explicit source cache before repeated evaluations:

```sh
export RSI_EVAL_SOURCE_CACHE="$PWD/.runtime/source-cache"
```

The downloader verifies the entire file against its SHA256 on every use. The cache stores public source files, not benchmark results. Each run retains its downloaded inputs and generated tasks, so allow disk space for these copies.

## Select and run

Use the existing target configuration and replace its benchmark section with:

```json
{
  "name": "livecodebench",
  "version": "release_v1",
  "task_ids": ["lcb-codeforces-1873_a", "lcb-leetcode-2727"]
}
```

Then use the normal interface:

```sh
rsi-eval preview --config examples/my-lcb-run.json
rsi-eval check --config examples/my-lcb-run.json
rsi-eval run --config examples/my-lcb-run.json
```

`my-lcb-run.json` must contain the full run configuration, including your target and output directory. The first selected task exercises standard input; the second exercises a `Solution` method. Select any other catalog IDs or omit `task_ids` to run the full release. The SDK uses the same selection through `BenchmarkSelection("livecodebench", "release_v1", ...)`.

The task tells the agent to write `/app/solution.py`. Its working directory contains the starter code and public examples. Harbor uploads the private cases and grader only after the agent phase ends. The agent may run its own checks on public examples, but it receives no hidden-test feedback during that attempt. Test and reference-solution files stay outside the image build context.

## Scores and failures

Each trial submits one final program. The verifier calls the unmodified official `run_test` function on all public and private tests, retaining the upstream function and standard-input behavior. Each case has a six-second execution limit. A separate worker applies the official outer deadline of `(6 + 1) * number_of_tests + 5` seconds. Harbor allows 600 seconds for the agent phase.

A program receives reward `1` only when every case passes. Wrong answers, syntax errors, runtime errors, missing submissions, and program timeouts receive `0`. Invalid datasets, unavailable grading dependencies, and unexpected verifier-process failures fail evaluation without inventing a reward. The verifier saves the official case results and diagnostic metadata in `livecodebench.json` beside Harbor's `reward.txt`.

Single-trial reward and pass@k are separate. The bundled aggregation requests pass@1 across complete, scored repetitions. A registered configuration can request larger k values, provided each task has enough scored samples. The run report retains the native Harbor summary and adds benchmark aggregation separately. Missing or failed infrastructure trials do not become failed samples.

## Integration references

The source dataset contains no canonical solution field. Two reference programs are available for `--oracle`:

- `lcb-codeforces-1873_a`: decide whether a three-letter permutation can become `abc` with at most one swap.
- `lcb-leetcode-2727`: count passenger ages strictly greater than 60.

These are **integration references, not official canonical solutions**. They solve the tasks from their statements and do not read test files or embed test answers. `--oracle` supports only these two IDs; normal agent evaluation supports all 400 tasks.

## Execution boundary

The submission and the official grader share a worker interpreter. The wrapper
returns results through a pipe after grading, without exposing result-file paths
in the program arguments. This removes an added result-file forgery path; it does
not turn the upstream grader into a sandbox for hostile code.
