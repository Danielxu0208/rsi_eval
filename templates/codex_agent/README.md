This is the built-in target for the Codex variants of Meta-Harness and RRSI.
Harbor loads `rsi_eval.harbor_codex:Agent` with this directory as `artifact_dir`.
Each candidate can edit `agent.py`, `prompt.md`, and helper scripts under `tools/`.

`agent.py` must provide `build_prompt(instruction, artifact_dir) -> str`. Optional
async `prepare_environment(environment, artifact_dir)` and
`after_run(environment, artifact_dir)` hooks run in the host evaluation process,
as with custom Harbor agents. Use this template with trusted candidate code.
The environment object is Harbor's task environment: commands passed to
`environment.exec` and uploaded helper scripts execute in that container.
The default template launches one Codex CLI session per trial, with native
Codex subagents disabled so its token accounting covers that complete session.
Other execution architectures can use a separate custom Harbor agent entrypoint.
The seed disables personal app/plugin/browser integrations and external web
search by default, and ignores user CLI configuration. It accepts task-local
helper scripts and task-declared MCP services (stdio and streamable HTTP).
Task MCP configuration comes from the benchmark; the seed cannot replace it
with personal services. A different interaction protocol can use a custom target.

Authentication, benchmark tests, task data, model choice, and usage accounting
belong to the runner. Do not put credentials or task-specific solutions in a
candidate. The runner records the candidate content hash and rendered prompt so
evaluation can verify that a proposed change was actually loaded.
