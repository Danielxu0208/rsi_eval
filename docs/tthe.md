# TTHE with Codex

> Retired integration. TTHE has been removed from the active package at the
> user's request. This guide and its validation links are retained as historical
> evidence; the installation and execution commands below no longer apply.

This optional integration runs the released TTHE LiveCodeBench search loop with a
Codex subscription. It evolves Python harnesses around one fixed solver model.
Each batch exposes problem statements, public samples, execution traces, and
self-generated checks. Hidden tests remain outside the search workspace.

The source pin is
[`junnie00/TTHE@69a614d`](https://github.com/junnie00/TTHE/tree/69a614d04f2a7f3eacb64a63193078ab4be56ba5).
The current adapter implements **LiveCodeBench `release_v1`**, including stdin and
functional tasks. Omitting `task_ids` selects all 400 problems in that release.
The upstream default is `test6`; this adapter passes the selected version through
the optimizer, proposer verification scripts, and judge prompts.

The upstream repository also contains SQL, DS-1000, and SWE domains. These domains
do not have execution adapters here. This integration does not claim their scores
or compatibility with every benchmark in the main catalog.

## Prepare and run

Use a source checkout of RSI Eval, Python 3.12, Docker, and the dedicated Codex
subscription login described in the main README. The examples use the repository
virtual environment. On this project's Mac setup, first run
`source scripts/mac-env.sh`. Linux users can use their working Docker context.

Before preparation, edit `integrations/tthe/acceptance.json` to set your available
`codex.model`, dedicated `codex.auth_dir`, and installed `codex.expected_version`.
The supplied model and CLI version describe this repository's experiment; they
are not defaults for every account. Preparation saves the resolved configuration
in the output directory.

```bash
.venv/bin/python -m integrations.tthe prepare \
  --config integrations/tthe/acceptance.json \
  --output runs/tthe-example \
  --build-image

.venv/bin/python -m integrations.tthe check --output runs/tthe-example
.venv/bin/python -m integrations.tthe check --output runs/tthe-example --live

.venv/bin/python -m integrations.tthe run --output runs/tthe-example
```

Preparation exports the exact upstream commit, downloads hash-verified benchmark
data, creates the public workspace, and optionally builds the Docker image. It
makes no model calls. The source download includes the full upstream JSONL file,
about 1.2 GB, even for a small selection. `RSI_EVAL_SOURCE_CACHE` can point to an
existing hash-addressed source cache used by the benchmark materializer.

`check` validates source hashes, release identity, public data fields, and existing
commitments. `check --live` also checks Docker and the existing Codex login. It
verifies a configured CLI version, or records the detected version when
`expected_version` was null. A fixed version mismatch is an error. Neither command
invokes a model.

The supplied acceptance configuration uses two batches with one task each:
`lcb-leetcode-2727` followed by `lcb-leetcode-2879`. Each batch creates two branches
and performs one generation round. The seed is upstream `bare`. Its published
prompt assumes stdin, which gives the first functional task an opportunity to
expose an interface failure. The second problem, minimum changes to make K
semi-palindromes, adds multiple arguments, divisibility rules, and a harder dynamic
programming task. We selected this order from public statements, starter code,
and difficulty metadata. The method may then make a general harness correction. The
adapter does not force edits or guarantee that a particular model will fail.
Upstream `react` is also available through `initial_harness`. Edit the configuration
before preparation to choose a different ordered task list, batch size, population,
or number of rounds. A manifest is also supported. Task order affects this method
because the selected harness carries forward between batches.

All model calls use the same configured Codex model: solver, proposer, judge,
back-translation, and stress-input generation. Authentication files stay in the
dedicated Codex home outside the experiment. There is no API fallback. The default
limit is 128 Codex CLI invocations; set `max_model_calls` explicitly for larger runs.
Dollar cost remains unknown for subscription use.

The prepared provenance pins the model, CLI version and executable, default
reasoning effort, and model timeout. The solver cache carries that identity and
cannot cross into a different model configuration. Refreshing the dedicated login
is supported because credentials are external to those execution settings.
`propose_timeout` sets one wall-clock budget for each complete proposer or judge
session, including its model calls and public shell checks. Resource exhaustion
stops the run instead of becoming another prompt observation.

## What the adapter preserves

The candidate and selection loop comes directly from the pinned
`livecodebench/lcb_optimize.py`. The adapter retains:

- Separate proposer branches with an assigned parent and shared peer traces.
- Native proposer instructions, diversity roles, and candidate audit rules.
- Arbitrary Python harness changes, public test execution, stress probes, and
  back-translation through the original helper APIs.
- A judge pool containing every observed candidate, including earlier rounds.
- The incoming incumbent as the rollback option and fallback after an invalid choice.
- Selected harness inheritance across batches.
- Frozen solver response reuse keyed by prompt, settings, sequence, and sample slot.
  Deliberate repeated calls retain distinct samples.

The adapter replaces Claude Code tools with an explicit Codex JSON action loop.
Roles can read public files, write their assigned candidate or choice file, and run
public investigation commands. Every shell command and candidate import executes
inside Docker with no network, credentials, verifier data, or host repository mount.
The host brokers text requests to the fixed solver. Roles receive the original
method prompts and tool results, while the executor controls allowed writes.

Execution is serial to share a normal subscription login. Upstream
`thinking=False` maps to Codex `low` reasoning effort. Its `max_tokens` argument is
an advisory instruction because this CLI path has no equivalent hard output cap.
These are declared execution differences from the original solver setup.

## Observe, commit, then score

During search, a candidate returns code and a public execution trace. The judge
chooses from those observations. The adapter then copies the chosen harness and
its already-generated programs into `committed/batch-NNNN/` and records SHA256
hashes. Before committing, it checks that the harness and cached programs match
the original observation hashes. Each commitment seals a copy of those observation
receipts; resume and grading recheck the copies and their source records. It never
regenerates those programs for final scoring. Historical runs from before this
receipt format can be audited against their original observation files without
rewriting their commitments.

Hidden grading starts only after the **entire selected stream** has committed.
This prevents earlier hidden scores from influencing later batches. The scorer
runs RSI Eval's pinned native LiveCodeBench utility on both public and private
tests. Upstream TTHE's measurement helper uses private tests only and a simpler
comparison; the report labels this adapter's native benchmark grading protocol.

The search model cannot read the private dataset, commitments, scoring outputs,
or host paths. The container sees only the public directory. Tool-free Codex calls
receive explicitly supplied text and run in a separate scratch directory. This
separation applies to generated code as well as role prompts.

## Outputs and interruption

The experiment directory contains:

| Path | Contents |
|---|---|
| `provenance.json` | Source pin, benchmark source, immutable runtime hashes, declared adaptations |
| `public/livecodebench/logs/` | Native trace files, proposer tasks, judge tasks, selection logs |
| `lineage/`, `observations/` | Parent and child hashes and observed candidate output evidence |
| `action_logs/`, `model_calls/` | Role actions, Codex requests, trajectories, usage, failed attempts |
| `committed/` | Chosen harness and cached programs, hashed before hidden scoring |
| `scores/` | Original verifier outputs and receipts tied to program and grader hashes |
| `checkpoint.json`, `interruption.json` | Durable progress and authentication, quota, timeout, or execution failures |
| `result.json` | Per-task native outcomes, usage, batch order, selection evidence, acceptance checks |

Resume an interrupted experiment with:

```bash
.venv/bin/python -m integrations.tthe run --output runs/tthe-example --resume
```

Completed batch commitments remain fixed. An unfinished batch restarts from the
last selected harness, with the frozen solver cache retained. Authentication or
quota errors stop immediately. Renew the same dedicated login or wait for quota,
then resume. To retry grading after all batches commit, run:

```bash
.venv/bin/python -m integrations.tthe score --output runs/tthe-example
```

Scoring uses no model calls. Existing results are reusable only when their receipt
matches the committed program, harness, benchmark, grader inputs, and raw output.
Keep the prepared configuration unchanged; use a new output directory for a
different task stream or search setup.

The report separates completed evaluations from method acceptance. Acceptance
requires at least two batches, at least two branches, a changed candidate observed
in every batch, correct cross-batch inheritance, and grading of committed cached
outputs. This strict check is separate from whether the native method ran
successfully. A proposer or judge may keep the incumbent; a valid no-op does not
make the integration fail. Improvement in score is not required.

## Validation status

Offline tests execute the vendored, hash-checked upstream control loop with
deterministic model and container boundaries. They cover population lineage,
earlier-round selection, rollback, public-only prompts, version propagation,
commit tampering, observation-to-commit identity, sealed receipts, interruption
and resume, solver caching, and score receipts.
The fixture includes the upstream MIT license.

```bash
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest tests/test_tthe_integration.py -q
```

The real Docker worker has passed harness import plus stdin and functional public
test probes. These probes made no model calls. A real two-batch run with the
upstream `react` seed completed all four proposer branches, native judging,
cross-batch inheritance, and native grading of committed outputs. Both tasks
passed, using 30 Codex CLI calls. All proposals kept their parent unchanged, so
the report correctly marks method acceptance incomplete. This result is retained
in `runs/tthe-acceptance-20261010`; it is not evidence of executed harness changes.

The shipped `bare` example also completed a real run in
`runs/tthe-bare-functional-hard-20261010`. In the first batch, both branches made
real changes. The judge selected a harness that detects starter code and preserves
the required class and method signature. That exact harness carried into the
second batch, where both branches left it unchanged after public checks and the
judge kept the incumbent. All four requested branches were observed; the chosen
cached outputs passed both tasks' native grading after the complete stream
committed. The original observation hashes match both committed harnesses and
programs, as independently recorded in
`.runtime/method-expansion/tthe-evidence.json`.

This run used 39 Codex CLI calls: 4 for program generation and 35 for search
(21 proposer, 10 judge, and 4 back-translation calls). Total reported usage was
477,955 input tokens and 4,789 output tokens. Grading used no model calls. The
native method ran successfully, including an executed harness modification and
cross-batch inheritance. Its status remains `evaluated_acceptance_incomplete`
because the stricter changed-in-every-batch check is false. No further edits or
experiments were forced to satisfy that check. These two-task results do not
reproduce paper results or establish full-set model scores.
