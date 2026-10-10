# Meta-Harness with Codex

Run one iteration of scaffold evolution. The outer, unchanged Meta-Harness search
loop owns evaluation, history, frontier selection, and iteration scheduling.
You analyze trajectories and implement one new general-purpose candidate.

1. Read the frontier and evolution history supplied in the task prompt. Deep-read
   successful and failed trajectories in the current run. Read the baseline and
   previous candidates. Form one falsifiable mechanism-level hypothesis, including
   expected score and token/turn effects. Use an analysis subagent if available.
2. Copy one existing artifact directory `agents/PARENT/` to a new snake_case
   directory `agents/NAME/`. Implement the hypothesis there. Use one implementation
   subagent if available. Never modify or import other candidate artifacts.
3. Validate syntax locally. Do not run benchmarks: the outer loop calls the shared
   SDK for runtime smoke validation and scored evaluation.
4. Write the exact pending JSON path supplied by the task prompt:

```json
{
  "iteration": 1,
  "candidates": [{
    "name": "example_name",
    "import_path": "agents.example_name:AgentHarness",
    "hypothesis": "One falsifiable mechanism-level claim",
    "changes": "The concrete change",
    "expected_efficiency": "Expected token and turn impact"
  }]
}
```

Replace the iteration and name. `import_path` is the upstream history identifier;
the adapter resolves it to the artifact directory. No AgentHarness class is needed.
The actual Harbor entrypoint is `rsi_eval.harbor_codex:Agent`.

Each artifact must contain `agent.py` with
`build_prompt(instruction: str, artifact_dir: Path) -> str`. Optional async hooks
are `prepare_environment(environment, artifact_dir)` and
`after_run(environment, artifact_dir)`. Read the copied template for examples.
You may change Python hooks, prompts, and tools within your new artifact.
The underlying solver and proposer both use the configured Codex CLI/model.
Do not add other model providers or API-key fallback.

Produce exactly one changed candidate per iteration, even when current scores are
high. One mechanism per candidate. Changes must be general-purpose: never mention
benchmark task names or encode task answers, task-specific hints, or grader access.
Do not read hidden verifier files. Do not change the evaluator, upstream controller,
previous agents, skills, histories, logs, benchmark data, or the frontier. Only create
your new artifact and the requested pending JSON. Do not create other files.
The test selections are not part of proposer input and must not guide selection.

This is a Codex variant of the original method: Claude-specific subagent commands,
Terminus2 inheritance, KIRA prompts, and the original 89-task/two-trial settings are
replaced. History-driven proposals, one candidate, smoke validation, native frontier
updates, and the original search loop are retained.
