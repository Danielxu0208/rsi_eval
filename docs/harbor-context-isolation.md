# Docker build context isolation

The Docker backend uses a private copy of each task's build context. Its directory name includes the full context SHA256. Original task packages remain unchanged.

This prevents different Harbor tasks from sharing the same BuildKit local-source name. The issue affected Polyglot reference solutions whose encrypted files had identical paths, sizes, modes and timestamps, but different contents.

## Build cache behavior

[Buildx v0.37.0](https://github.com/docker/buildx/blob/v0.37.0/build/opt.go) derives its local-source shared key from the context directory's basename. Harbor normally names every context `environment`.

[BuildKit](https://github.com/moby/buildkit/blob/master/source/local/source.go) reuses local-source snapshots with that shared key. Its default [metadata comparison](https://github.com/tonistiigi/fsutil/blob/master/diff_containerd.go) can consider files unchanged without comparing their contents.

Files with matching relative paths, sizes, timestamps and modes can therefore
reuse stale content when their bytes differ. The guard includes content hashes
in the context directory name so those inputs have distinct source identities.

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
