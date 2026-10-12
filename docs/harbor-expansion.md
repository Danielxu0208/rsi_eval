# Additional Harbor benchmarks

The catalog adds three benchmark variants to the shared `evaluate()` interface.
Each uses Harbor 0.18.0 and accepts the existing Codex artifact target or another
Harbor agent. No Aider installation is required for the Polyglot port.

| SDK name | SDK version | Full task count | Registry source |
| --- | --- | ---: | --- |
| `aider-polyglot` | `1.0-harbor` | 225 | `aider/aider-polyglot` |
| `swe-bench-multilingual` | `1.0-harbor` | 300 | `swebench_multilingual@1.0` |
| `swe-bench-pro` | `1.0-harbor` | 731 | `scale-ai/swe-bench-pro` |

The SDK versions identify the bundled Harbor variants. They are local version
labels, not upstream release numbers. Results include protocol notes and source
provenance under `resolved.benchmark`.

## Fixed task identity

[catalog_harbor_expansion.json](../src/rsi_eval/data/catalog_harbor_expansion.json)
contains every task ID and its immutable source. The Polyglot and Pro entries
record registry dataset digests and individual package hashes.

Multilingual uses Git task references. Its registry snapshot contains 299 tasks
at commit `61366953049fd63a7dff54f67193bf39f5d1cde6` and `tokio-rs__tokio-4384`
at `19fb29e2b4d1e24615f620541b1a91461c00f5c5`. The catalog preserves both revisions.
Its `manifest-sha256:` reference identifies our complete task list, not a registry
package digest. The provenance records the hash encoding.

Adapter documentation is fixed to Harbor 0.18.0 commit
`527d50deb63a5d279e8c20593c18a2cbc7f61f9e`. The registry does not identify which
adapter revision generated each bundle. The task references determine executed
content; the documentation commit provides context.

## Polyglot protocol

The original Aider runner defaults to two edit/test attempts. A failed test
supplies feedback to the next edit in the same exercise. Its reports include
pass rates by attempt and edit-format correctness. See the
[native retry loop](https://github.com/Aider-AI/aider/blob/main/benchmark/benchmark.py)
and [benchmark instructions](https://github.com/Aider-AI/aider/blob/main/benchmark/README.md).

This variant runs a Harbor agent against an exercise and records its verifier's
binary reward. It does not implement Aider's retry controller or edit-format
metrics. `repetitions=2` creates two independent trials; it does not add a second
repair attempt with the first trial's test feedback.

The port changes instruction assembly, language environments, and some build
dependencies. Report results as **Aider Polyglot, Harbor port**, with the
candidate, model, task snapshot, and execution settings.
See the [pinned adapter documentation](https://github.com/harbor-framework/harbor/blob/527d50deb63a5d279e8c20593c18a2cbc7f61f9e/adapters/aider_polyglot/README.md).

## SWE protocols

SWE-bench Multilingual covers 300 issues across nine programming languages.
The original benchmark requires the issue-specific failing tests to pass while
preserving existing passing tests. The Harbor adapter uses the official
SWE-bench grading functions and includes available `hints_text` in its prompt.
Record this task instruction along with the chosen agent and budget.
See the [original protocol](https://www.swebench.com/multilingual.html) and
[pinned adapter](https://github.com/harbor-framework/harbor/blob/527d50deb63a5d279e8c20593c18a2cbc7f61f9e/adapters/swebench_multilingual/README.md).

SWE-bench Pro exposes 731 public instances. Its official harness accepts patches
from the generator of your choice. The Harbor port wraps the supplied
`run_script.sh` and `parser.py`, then grades required failing-to-passing and
passing-to-passing tests. It also changes execution details, including Jest
worker and exit flags. This catalog covers the public release; it does not
include the held-out or commercial evaluations.
See the [official patch interface](https://github.com/scaleapi/SWE-bench_Pro-os#usage)
and [pinned adapter](https://github.com/harbor-framework/harbor/blob/527d50deb63a5d279e8c20593c18a2cbc7f61f9e/adapters/swebenchpro/README.md).

These catalogs preserve task-defined environments and agent/verifier timeouts.
They do not impose a universal attempt count or context window. Independent
repetitions, model settings, and any outer job deadline belong to the recorded
run configuration.

## Select a task

Keep the target and options from an existing run configuration. Replace its
`benchmark` field with a selection such as:

```json
{
  "name": "aider-polyglot",
  "version": "1.0-harbor",
  "task_ids": ["polyglot_python_dot-dsl"]
}
```

The following IDs exist in the fixed catalog and can be used for small runs.

| Variant | Task ID |
| --- | --- |
| Polyglot, Python | `polyglot_python_dot-dsl` |
| Polyglot, Go | `polyglot_go_error-handling` |
| Multilingual, JavaScript | `preactjs__preact-3345` |
| Pro, Python, current example | `instance_internetarchive__openlibrary-00bec1e7c8f3272c469a58e1377df03f955ed478-v13642507b4fc1f8d234172bf8129942da2c2ca26` |
| Pro, Go | `instance_flipt-io__flipt-a0cbc0cb65ae601270bdbe3f5313e2dfd49c80e4` |

Use `rsi-eval preview --config <run.json>` to inspect resolved references without
starting a model or container. A task manifest can replace `task_ids`; its entries
must match this variant's catalog. The calling method chooses evolve and test
roles independently.

All three ports produce binary verifier rewards and native Harbor results.
The SDK retains trial status, raw job metrics, token usage, and trajectory paths.
An unsuccessful solution can have a valid reward of zero. Missing verification
and infrastructure failures remain distinct from that measured outcome.

## Runtime requirements

Follow the [runtime setup](../README.md) for Harbor, Docker, and the dedicated
Codex login. These public task sources do not require an additional model API
key when using the existing Codex target. Native Pro evaluation can use Modal,
but this Harbor configuration uses the selected environment backend.

Pro's published base images target `linux/amd64`. Apple Silicon hosts require
emulation. Polyglot tasks use several language toolchains; Java and C++ builds
can require more memory and time than a Python exercise. Preserve the bundle's
resource requirements when selecting a machine or an outer job deadline.
