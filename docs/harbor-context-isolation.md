# Docker build context isolation

The Docker backend uses a private copy of each task's build context. Its directory name includes the full context SHA256. Original task packages remain unchanged.

This prevents different Harbor tasks from sharing the same BuildKit local-source name. The issue affected Polyglot reference solutions whose encrypted files had identical paths, sizes, modes and timestamps, but different contents.

## Evidence and cause

Two runs of the Python `dot-dsl` and Go `error-handling` oracle pair produced rewards of 1 and 0. The Go oracle log reported installing the Python solution. A separate Go run produced reward 1.

The cached Go payload contained the correct Go solution. The Go verifier did not overwrite the editable source file. Host oracle logs had different inodes and write times.

[Buildx v0.37.0](https://github.com/docker/buildx/blob/v0.37.0/build/opt.go) derives its local-source shared key from the context directory's basename. Harbor normally names every context `environment`.

[BuildKit](https://github.com/moby/buildkit/blob/master/source/local/source.go) reuses local-source snapshots with that shared key. Its default [metadata comparison](https://github.com/tonistiigi/fsutil/blob/master/diff_containerd.go) can consider files unchanged without comparing their contents.

Both encrypted payloads were 10,272 bytes with timestamp zero and matching file modes. Their relative path was `workspace/.oracle/solution.enc`, but their SHA256 values differed:

| Task | Payload SHA256 |
| --- | --- |
| Python `dot-dsl` | `e2ec87f76b0e62bde3c6948839106e8000f406fe67e8392456caf41bd052cf0a` |
| Go `error-handling` | `4b9ed3134aa07da7f8acb31a4f1e5025de34b92433a21c195e50b28feb5b5f1c` |

This combination explains why an unchanged-looking payload could survive the next context transfer.

## Guard behavior

`rsi_eval.harbor_docker:Environment` extends Harbor's Docker environment. The current guard version is `rsi-context-v2`. For default Dockerfile tasks, it performs these steps:

- Hash every context entry, including hidden files, file modes and relative symlink targets.
- Copy the context to `trial/build-context/environment-<sha256>` without changing file contents, modes or timestamps.
- Use that directory for both the build context and Compose project directory.
- Append the guard version to Harbor's generated image tag.
- Save source path, copied path and context hash in `trial/build-context/provenance.json`.

Task IDs, package references, instructions, oracle scripts, verifier scripts and resource settings retain their original values. Prebuilt image references also remain unchanged. Prebuilt tasks without a local environment directory bypass copying and record that decision.

Relative symlinks inside the context are preserved. Escaping links, absolute links and special files are rejected before copying. The guard never reads an external link target.

Custom Compose files are normalized by Docker Compose using Harbor's original
project directory, merge order and runtime environment. The guard then copies
each local build context, external Dockerfile and local additional context into
a content-specific directory. Runtime bind mounts, env files, resource layers,
commands and service relationships retain their normalized meaning. Unsupported
remote build contexts fail explicitly before a build.

The normalized runtime document may contain private environment values. It is
held in a mode-0600 temporary file for the invocation and deleted afterward.
Public provenance records only build sources, hashes and Compose file identities.

## Validation

Offline tests cover two hidden payloads with equal metadata and different contents, unchanged source files, preserved symlinks, Compose configuration and prebuilt image references.

The same Python/Go oracle pair passed in one job after isolation. These local run directories preserve the evidence:

| Run | Python reward | Go reward | Context isolation |
| --- | --- | --- | --- |
| `runs/expansion-polyglot-oracle-01` | 1 | 0 | Disabled |
| `runs/expansion-polyglot-pair-oracle-03` | 1 | 0 | Disabled |
| `runs/expansion-polyglot-pair-oracle-04` | 1 | 1 | Enabled |

In the final run, the Python log records `example.py → dot_dsl.py`. The Go log records `example.go → error_handling.go`. Each trial's `build-context/provenance.json` records its original package path and copied context hash.

The original pair run validates the earlier default Dockerfile implementation.
The 2026-10-11 v2 regression additionally builds two real custom Compose tasks
with equal-size, equal-timestamp hidden files and different contents. It covers
relative external Dockerfiles, additional contexts, bind mounts, env files,
extra override layers, literal dollar values and cleanup. See the
[v2 evidence](validation/compose-isolation-20261011.json) and
`scripts/validate_compose_isolation.py`. No model or grader was called.
