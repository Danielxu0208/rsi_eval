# Freeze dependencies and runtime assets

Host dependencies and benchmark images have separate locks. A host lock fixes
Python package versions for a particular platform. A runtime snapshot preserves
prepared task files and the image content IDs observed during a new evaluation.
Neither changes the benchmark's scorer or supplies a paid judge credential.

## Host package locks

`method prepare` records the environment inventory and pip installation report.
With working pip, it also writes `dependency-lock.json` after `pip check`; reuse
that file for another preparation. A reused environment without pip retains its
inventory and an explicit explanation that no lock was generated.
Pass `--dependency-lock PATH` to install and verify an exact recorded inventory:

The bundled-lock example below runs from the repository or unpacked source
distribution. Installed users can pass a previous experiment's
`dependency-lock.json` instead.

```sh
rsi-eval method prepare --method ahe --profile profile.json \
  --experiment runs/ahe-locked \
  --dependency-lock integrations/locks/macos-arm64-py312/ahe.json
```

The bundled locks cover all four methods on macOS arm64 with Python 3.12.15.
They were installed into new environments, checked with `pip check`, and replayed
into another set of new environments. Native imports and prerequisite checks
passed without model calls. Each method has its own lock. A lock for another
platform or Python minor version is rejected. The normal preparation path can
resolve dependencies on that host and record a new inventory.

These locks pin package versions. Installation reports retain the downloaded
artifact hashes; the locks do not preserve a private package mirror or promise
that an index will keep serving every artifact. Operating-system libraries and
benchmark dependencies are preserved by the image workflow below.

## Capture a new evaluation

Add this option before the evaluation whose runtime you want to retain:

```json
{
  "options": {
    "retain_runtime_images": true
  }
}
```

For a method, put it in `run_options`. The option adds content-specific local
image tags so Harbor's normal container cleanup leaves the observed images
available for capture. It does not keep containers running. Without this option,
capture can only succeed if the observed images are still in the Docker store.

After an evaluation has started every selected task environment:

```sh
rsi-eval runtime capture --run runs/candidate-001 --output snapshots/runtime-001
rsi-eval runtime check --snapshot snapshots/runtime-001
```

Capture supports source-backed adapters and pinned Harbor package or Git tasks.
It verifies the candidate and task identities, copies every prepared task,
and saves the observed service images to `images.tar`. This includes private
grader sidecars declared in the task's Compose environment, copied assets, build
outputs, operating-system packages, and resolved transitive dependencies already
present in those images. Files, modes, image IDs and the archive have integrity
checks. The snapshot does not execute an agent or grader.

`--no-save-images` stores task files and image IDs without the archive. That mode
requires the existing Docker image store and is not a portable image backup.
Snapshots can be large. Remove retained `rsi-eval-runtime:<image-id>` tags only
after you no longer need those local images or have a verified saved archive.

Capture requires contemporaneous image evidence. Harbor package and Git tasks
also require the task path and complete file identity recorded at startup. Keep
their downloaded task directories until capture finishes. It refuses old runs
without this evidence, missing task environments, inconsistent task files or
images across repetitions, and external host bind mounts or volumes that were
not part of the task. It does not reconstruct historical resources.

## Replay without materializing or rebuilding

Set `options.runtime_snapshot` to the snapshot directory in an ordinary run
configuration, or set `run_options.runtime_snapshot` in a method configuration:

```json
{
  "options": {
    "runtime_snapshot": "snapshots/runtime-001"
  }
}
```

An individual evolve, smoke or test selection can also set `runtime_snapshot`.
Use a separate snapshot for each benchmark configuration. The requested tasks
must be contained in the snapshot and match its pinned catalog identities.
Omitting task IDs still selects the complete catalog; a subset snapshot cannot
silently reduce that selection.

On another compatible Docker host, copy the complete snapshot directory and run:

```sh
rsi-eval runtime restore --snapshot snapshots/runtime-001
rsi-eval runtime check --snapshot snapshots/runtime-001
rsi-eval run --config frozen-evaluation.json
```

Replay copies the verified tasks and uses the recorded content IDs for every
Compose service. It removes the build graph, disables pulls and preserves runtime
commands, mounts, resources and networking. It does not rerun apt, pip or asset
bootstrap steps. Current hardware and judge credentials remain required. Source
asset paths and credential-file locations may be rebound; grader settings and
other runtime options must match the captured configuration.

The snapshot covers task files and images, not external web services, model
behavior, credentials or later agent-installed packages. Harbor package and Git
replay use the captured local task files, preserving their instructions and
verifiers without downloading the original source again. An incompatible CPU or
missing accelerator still fails explicitly.

## Validation

`scripts/validate_runtime_snapshot.py` runs a real Docker fixture through the SDK,
captures and restores its image archive, removes its source assets, changes a
build argument, and evaluates it again from the frozen runtime. Both verifiers
must observe the original image and asset bytes. This is a synthetic integration
check with zero model and judge calls, not a benchmark score.
The [2026-10-11 record](validation/runtime-snapshot-20261011.json) contains
the successful fixture results and implementation hashes.

`scripts/validate_registry_snapshot.py` checks a real pinned Harbor package task.
The TB 2.0 `log-summary-date-ranges` oracle passed before capture and after
restoration, with the same image content ID and unchanged task files. The
[registry record](validation/registry-snapshot-20261011.json) also retains the
earlier parser failure that exposed the distinction between a native task name
and its copied directory name. Package/Git offline tests additionally delete
the original downloaded task cache before replaying.
