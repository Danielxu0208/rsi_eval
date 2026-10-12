# Prepare, configure and recover methods

The optional command layer packages four existing method integrations. It
prepares their inputs and invokes their native launchers. It does not own their
search loops or require a MethodAdapter subclass. A user-owned method can call
`rsi_eval.evaluate()` directly without using these commands.

## Prepare once

Install the core wheel or `pip install .` with Python 3.12. Complete the dedicated
Codex login and Docker setup in the [README](../README.md), then run:

```sh
rsi-eval configure --model gpt-6-astra \
  --auth-dir "$HOME/.codex-rsi-eval" --output profile.json
rsi-eval method prepare --method meta-harness \
  --profile profile.json --experiment runs/meta-example
rsi-eval method check --experiment runs/meta-example
rsi-eval method run --experiment runs/meta-example
```

Use a model available to your account. `configure --codex /path/to/codex` supports
a CLI outside `PATH`; it records the probed CLI version. The profile contains
only settings and the authentication directory's path, never credentials.

`prepare` creates an isolated environment under the experiment, installs Harbor
0.18.0 and the method's requirements, obtains the pinned upstream revision, and
freezes the SDK, integration, configuration and seed. It calls no model. `check`
also calls no model; it checks versions, authentication shape and task runtime
prerequisites. It cannot establish remaining subscription quota or task success.
`run` invokes the method and can consume model quota.

The accepted method names are `meta-harness`, `rrsi`, `ahe` and `harnessx`. The
wheel includes the adapters, configuration examples and Codex template. It does
not include upstream repositories, benchmark assets or method dependencies.

Pass `--source /path/to/upstream` to use an existing clone. Pass
`--environment /path/to/venv --no-install` to use an environment that already
contains the method dependencies. Repeating the same prepare command reuses a
completed preparation. After a preparation failure, retry the same command;
recognized incomplete managed checkouts are archived before rebuilding them.
Changed inputs require a new experiment directory.

The prepared environment's installed distributions are recorded and checked on
launch. Keep it dedicated to the experiment. Do not edit its frozen configuration,
runtime or seed after preparation; create a new experiment for another variant.
New managed experiments support receipt-checked export and import. See the
relocation instructions below. Older experiments without the required identity
records remain outside this migration path.

## Choose tasks

Pass `--config experiment.json` alongside the shared profile to replace the
bundled example. The explicitly supplied profile overrides its Codex settings.
Paths resolve relative to the file in which they were declared.

For example, this is a complete small Meta-Harness configuration to use with
`--profile profile.json`:

```json
{
  "iterations": 2,
  "run_options": {"environment": "docker", "timeout": 1800},
  "evolve": {
    "name": "terminal-bench", "version": "2.0",
    "task_ids": ["extract-elf"]
  },
  "test": [
    {"name": "terminal-bench", "version": "2.1",
     "task_ids": ["extract-elf"]}
  ]
}
```

Every selection supports `task_ids`, a `manifest` containing a JSON task list,
or neither to select the complete pinned configuration. `evolve` and `test` are
method-level choices, not labels assigned by the benchmark catalog. Selections
may provide `benchmark_runtime` to replace shared runtime settings for that
call, or `runtime_snapshot` to use an explicitly frozen task/image snapshot.
Missing judge credentials, assets or hardware fail explicitly. AHE checks every
configured final-test selection as well as evolve before starting the method.

For Meta-Harness, a manifest or full-set evolve selection needs an explicit
`smoke` selection, or the explicit `skip_smoke` option. The implicit first-task
smoke applies when evolve lists `task_ids`. Smoke is a separate evaluation and
does not reduce the scored evolve selection.

RRSI resolves and freezes evolve/smoke task lists during preparation, including
manifest and full-set selections. Its candidate worktrees inherit this exact
configuration. Later edits to the original manifest do not change that run.

AHE supports original task IDs containing `/`. Filesystem aliases are stable
SHA256 names, with a saved reverse mapping. Native statistics, debugger queries,
history and delayed attribution continue to refer to the original task IDs.

The methods retain different native settings: Meta-Harness uses `iterations`,
AHE and HarnessX use `rounds`, and RRSI uses its `rrsi` settings such as `T`, `m`
and `k`. Their examples and detailed guides document these fields. The manager
does not silently translate one method's controls into another's.

## Choose an objective

Raw rewards and verifier output always retain the benchmark's meaning. By
default the methods use their existing objectives. Set a top-level
`method_policy` in the experiment configuration to declare another variant:

```json
{
  "method_policy": {
    "variant": "drop-f1-at-least-0.8",
    "score": {
      "kind": "threshold", "metric": "reward",
      "threshold": 0.8, "direction": "maximize"
    }
  }
}
```

This turns a measured DROP F1 of at least 0.8 into a method score of 1 and a
lower F1 into 0. It never changes the SDK's raw F1. Scores from missing trials,
authentication failures or environment failures cannot be mapped into valid
measurements.

| Method | Evolve objective and selection constraints |
| --- | --- |
| Meta-Harness | Binary mapped task scores; selection stays within measured candidates that passed native gates |
| AHE | Binary mapped task scores for the native debugger/best tracker; explicit best-candidate selection uses measured snapshots and affects rollback guidance and final export |
| RRSI | Comparable scores in [0,1]; critic, smoke, completeness, noise and cost constraints remain mandatory |
| HarnessX | Finite scores; optional selection runs after native validity, novelty, evidence and smoke gates |

Meta-Harness and AHE need an explicit score mapping for continuous or
heterogeneous objectives. RRSI accepts DROP F1 directly but needs a declared
mapping for Frontier-Eng objectives. HarnessX's default promotion follows its
native gates; score improvement is not a default promotion requirement.

Supported score policies are:

- `threshold`: a dotted trial field such as `reward` or `metrics.f1`, threshold,
  and `direction` (`maximize` or `minimize`); returns 0 or 1.
- `linear`: a field, `min`, `max` and direction; clips and maps to [0,1]. The
  experimenter must choose meaningful bounds for the task objective.
- `python`: a standalone file and function. It receives
  `{"trial": ..., "benchmark": ...}` and returns a finite number satisfying the
  method's score range.

An optional Python selection policy receives eligible candidates, the native
choice, and method-specific context:

```json
{
  "method_policy": {
    "variant": "my-explicit-selection",
    "selection": {"kind": "python", "path": "policy.py", "function": "choose"}
  }
}
```

```python
# policy.py: this example delegates to the native choice.
def choose(payload):
    return payload["native_choice"]
```

Return an eligible candidate's `id`, or `None` to retain the incumbent when the
calling method supports that decision. A final export that requires a selected
artifact rejects `None`. Callbacks cannot nominate an unmeasured candidate or
bypass native gates. Each method records its selection payload and decision;
candidate fields other than `id` are method-specific.

Policy files are trusted Python code, not sandboxed plugins. Keep them outside
evolvable candidate directories and use standalone standard-library imports.
Preparation embeds their source and hash in the frozen experiment. Later edits
to the original file do not change the policy. A changed policy is a distinct
method variant and must be reported as such.

## Execution compatibility

Meta-Harness, RRSI and AHE share the Codex wrapper's Harbor execution path,
including task-provided MCP. HarnessX preserves its native processors and tool
loop and uses a container-local MCP bridge. MCP schemas, responses and traces
are preserved. HarnessX attaches static PNG, JPEG and WebP images to Codex while
retaining their original message and tool-call positions. Raw media and oversized
responses remain in task-readable files. Native context limits still apply;
audio, video and animated images are unsupported. See the
[HarnessX media guide](harnessx.md#task-tools) for supported formats and limits.

The methods support task-provided MCP tools and explicit objective adaptation.
Native judge credentials, hardware, task services and the chosen agent's
capabilities determine the requirements of an experiment. Keep publisher usage
terms in force when choosing evolve data.

## Recover work

`completed` means the requested native method workflow settled and its required
measurements are present. A rejected candidate or a valid KEEP can end a round
normally. Authentication, infrastructure and missing-result failures stop
execution.

Resolve the recorded authentication, quota or environment problem, then run:

```sh
rsi-eval method resume --experiment runs/meta-example
```

The command invokes each method's own recovery path:

| Method | Recovery behavior |
| --- | --- |
| Meta-Harness | Restore durable stage/history/frontier checkpoints; reuse valid completed evaluations; archive incomplete proposer work |
| RRSI | Infer the first unsettled round; archive recognized authentication/quota STOP records before explicit retry; refuse unknown manual STOP markers |
| AHE | Infer the latest saved iteration input, perform native rollback, archive superseded work and replay that iteration |
| HarnessX | Restore its state and journal, preserve attempts and continue pending work through its native launcher |

Method recovery can repeat calls within an interrupted native round. Previous
attempts and their available usage remain recorded. No command automatically
switches from subscription access to an API provider.

For a standalone SDK evaluation:

```sh
rsi-eval resume --output runs/candidate-001
```

```python
from pathlib import Path
from rsi_eval import resume

result = resume(Path("runs/candidate-001"))
```

Core recovery validates the saved candidate, benchmark, runtime settings and
execution-code identity. It retries only missing task repetitions; valid zero
rewards are completed measurements. Every invocation makes at most one new
attempt per missing slot. Prior raw artifacts remain unchanged, receipts detect
evidence tampering, and a merged native view lets method code read the selected
trial evidence. Usage includes failed attempts when known; missing usage stays
unknown with an observed subtotal.

`evaluate(..., resume=True)` or `run --config CONFIG --resume` additionally checks
that the supplied request still matches the original. `resume(output_dir)` uses
the stored candidate even if its former source directory changed. These are
explicit retries, not a shared result cache.

Older experiments without the required checkpoint and execution identity are
not silently converted. Preserve their original evidence and use their native
recovery instructions or start a new experiment.

## Test the selected artifact

All four managed methods expose the same independent test command:

```sh
rsi-eval method test --experiment runs/meta-example --output runs/meta-test
rsi-eval method test --experiment runs/meta-example \
  --selection selected-tests.json --output runs/meta-other-tests
```

`selected-tests.json` contains one ordinary benchmark selection or a list of
selections. Without it, the command uses the experiment's configured `test`
selections. It loads the native selected candidate, verifies its measured hash,
and runs the frozen SDK and integration. It does not run evolution again or
change native history. An unmeasured, missing or modified selected artifact is
rejected. Add `--resume` to continue the same independent test after resolving
an interruption. Changed selections require a new output directory.

Before starting any evaluation, the command checks the entire batch: task
selection, effective runtime options, required assets and credentials, tool
versions, and existing evaluation records when resuming. It writes every
selection's check result to `preflight.json`. A failed check returns
`preflight_failed` and starts no evaluations. A failed resume preflight preserves
the previous `method-test.json` and benchmark results. Fix an external prerequisite
and retry with `--resume`; use a new output directory when changing the request.
These checks make no model calls or container runs. They cannot guarantee that
credentials remain valid, quota is available, or task environments will succeed
during execution. Independent tests preserve raw scores and require no method
selection policy or score mapping.

Prepared experiments keep their frozen runtime. The batch preflight applies to
experiments prepared with this version; upgrading the installed SDK does not
rewrite older experiment snapshots.

## Move a recorded experiment

Stop its active process before exporting. The manager refuses a locked experiment.

```sh
rsi-eval experiment export --experiment runs/meta-example --output exports/meta-example
rsi-eval experiment import --bundle exports/meta-example \
  --experiment runs/meta-moved --environment /path/to/matching-method-venv \
  --auth-dir "$HOME/.codex-rsi-eval"
rsi-eval method check --experiment runs/meta-moved
rsi-eval method resume --experiment runs/meta-moved
```

The directory bundle excludes credentials and virtual environments. Import
requires the same host platform and package inventory, verifies every bundled
file, and rebinds the environment and authentication locations. Use the saved
dependency lock to create a matching environment when needed. External assets
must retain their recorded contents. Pass `--path-map paths.json` with a JSON
object mapping original absolute paths to new absolute paths when they move.
`--codex` rebinds an explicitly recorded executable path. If the original profile
used the bare command `codex`, keep that command available on `PATH` instead.

Candidate code, frozen SDK files and raw SDK/Harbor evidence retain their bytes.
An import receipt preserves original control files and records path changes.
Git worktree administrative links are rebased without changing RRSI commits.
Native checkpoint and candidate checks still apply after import. This mechanism
supports new experiments prepared with relocation-aware runtimes; it does not
repair old incomplete checkpoints or claim cross-platform migration.

See [runtime snapshots](runtime-snapshots.md) for host dependency locks and
explicit archives of benchmark images and downloaded assets.

## Native entry points and records

The command layer records `method.json`, `configuration.json`, `runtime/`,
`upstream-source/`, an optional prepared `checkout/`, and native outputs under
`run/`. `method.json` records the resolved native invocation inputs. The
existing launchers remain available for advanced controls:

- [Meta-Harness](../integrations/meta_harness/README.md)
- [RRSI](../integrations/rrsi/README.md)
- [AHE](ahe.md)
- [HarnessX](harnessx.md)

RRSI's managed `run` invokes its native evolution;
independent final tests use the shared `method test` command.
The other bundled launchers execute their configured final tests as part of
their run. This command layer preserves that distinction.

GDPval and TTHE are no longer active integrations.
