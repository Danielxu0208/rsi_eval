# DROP and Frontier-Eng validation

This expansion adds two profiles through the existing SDK and CLI. It runs small
reference and Codex checks with the existing subscription. It does not repeat
Meta-Harness or RRSI search loops, measure full-dataset scores, or reproduce papers.
The [machine-readable record](validation-drop-frontier.json) includes successful
and failed attempts, report hashes, candidate identities, usage and trajectories.

**Small-subset acceptance passed on macOS with Colima and a Codex subscription.**
Both profiles completed an oracle/reference job and a separate real Codex job.
The evidence contains five jobs and ten trials, including the failed initial
Frontier environment attempt. The four accepted jobs contain eight trials.

| Configuration | Supported selection | Acceptance selection |
| --- | --- | --- |
| `drop@1.0-agent` | All 86,945 public train/dev records | Two dev records: one span and one numeric answer |
| `frontier-eng@cpu-subset-bafa7d5-v1` | Two reviewed CPU tasks from the 47-task v1 matrix | Both supported tasks |

DROP retains nine training records with empty reference answers and both records
sharing one dev query ID. Its hidden test set is not included. Frontier-Eng's
two-task profile is neither the complete v1 collection nor v1-lite. Unsupported
tasks remain listed in its coverage manifest. See [DROP](drop.md) and
[Frontier-Eng](frontier-eng.md) for source pins and protocol details.

## Measured execution

The host is an Apple Silicon Mac running Colima. Jobs use Harbor `0.18.0` and
run serially. Codex uses `gpt-6-astra`, medium reasoning and CLI
`0.162.0-alpha.2`, with the dedicated subscription login. No API key is used;
subscription dollar cost remains unknown.

DROP's oracle and Codex jobs both completed. Each selected record scored
**EM 1 and F1 1**:

- `dev-f37e81fa-ef7b-4583-b671-762fc433faa9`
- `dev-ac6ba235-3024-4f63-a6ab-730a14def4cb`

The oracle submits a dataset answer to check transport and the unchanged scorer.
The separate Codex run produces its own answer file. These two records are not
an accuracy estimate for the release.

Frontier-Eng's initial programs and changed Codex candidates all completed with
`valid=1`:

| Task | Initial native score | Selected Codex score | Changed proposals evaluated through feedback |
| --- | ---: | ---: | ---: |
| `InventoryOptimization/disruption_eoqd` | 0.36423022249600623 | 0.6419161854248078 | 1 |
| `Robotics/PIDTuning` | 0.036626766599899996 | 0.1553177989212411 | 2 |

Both trajectories contain actual source changes followed by feedback calls.
The broker preserved the changed source hashes, native grades and final selected
source. Each final diagnostic rerun completed. These single trials establish a
working integration, not reliable improvement across the suite. Native scores
have different meanings across tasks, so the report provides no combined score.

Accepted directories under `runs/` are `drop-frontier-drop-oracle-01`,
`drop-frontier-drop-codex-01`, `drop-frontier-frontier-oracle-02`, and
`drop-frontier-frontier-codex-01`. The Frontier pair used identical stable source
and protocol hashes and the current runtime bytes. Per-run verification tokens
make the generated task hashes different by design.

The first Frontier oracle attempt failed before solving because an inherited
`DOCKER_DEFAULT_PLATFORM=linux/amd64` conflicted with the native ARM grader.
The successful attempt cleared that setting and explicitly used
`RSI_EVAL_FRONTIER_PLATFORM=linux/arm64`. The adapter now detects this conflict
before downloading task sources. The failed attempt remains in the evidence.

The Frontier feedback broker records each submitted source, native metrics and
the best feasible candidate within its budget. Its maximum is 99 feedback calls
and one final proposal, plus the separate initial baseline. A diagnostic rerun
checks the selected source without supplying more optimization feedback. The
agent has a 600-second wall-clock cap; the actual feedback count is recorded.
This is the declared `harbor-feedback-100-v1` protocol, not the upstream
OpenEvolve loop. Original numerical graders remain unchanged.

## Offline and packaging checks

The offline suite passed **290 tests and four subtests**, with one EngDesign
scientific-dependency test skipped. That test passed separately in the existing
NumPy/SciPy/Matplotlib environment. Tests exercise official DROP partial credit,
malformed submissions, source identity, Frontier budgets and best-candidate
selection, feedback privacy, native invalid scores, and incomplete/error results.

The core still has no runtime dependencies. Scientific dependencies run inside
task containers or the optional test environment. A clean wheel installation
listed all 11 profiles and previewed all eight expansion examples from the source
archive without Harbor, NumPy, SciPy or Pydantic. The README's offline demo
completed with zero model calls. The archives contain both new adapters, catalogs,
grader fixtures and their licenses. See
[packaging evidence](validation-drop-frontier-packaging.json).

## Method compatibility

Custom methods can call `evaluate()` with either profile and choose their own
fitness from the returned metrics. The bundled method examples keep their
original selection semantics:

| Example | DROP as evolve set | Frontier-Eng as evolve set | Final evaluation through SDK |
| --- | --- | --- | --- |
| Meta-Harness | Rejected: native selector expects binary pass rates | Rejected: native selector expects binary pass rates | Both supported |
| RRSI | Mean F1 fits its bounded score contract | Requires an explicit task-specific fitness adaptation | Both supported |

The guards run before model calls. They prevent fractional F1 from becoming a
binary pass rate and unrelated engineering objectives from becoming one fitness
silently. These are limits of the bundled examples; the SDK imposes no method
base class or fixed evolve/test roles. No real method search on these new
profiles is claimed by this acceptance record.
