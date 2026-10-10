# HarnessX integration

This optional Codex adaptation runs the pinned public HarnessX harness evolution workflow. It preserves native processors, tools, post-flight gates, synthetic replay and journal history. Task-provided MCP tools execute inside the Harbor environment.

Use the shared CLI after installing RSI Eval and configuring a dedicated Codex subscription profile:

```sh
rsi-eval method prepare --method harnessx --profile codex-profile.json --experiment experiments/harnessx
rsi-eval method check --experiment experiments/harnessx
rsi-eval method run --experiment experiments/harnessx
rsi-eval method resume --experiment experiments/harnessx
```

Pass `--config experiment.json` to `prepare` to choose your seed, evolve and test selections, resource limits, or explicit score and selection policies. Without a policy, the published TB2 workflow promotes candidates after its native gates and does not compare scores.

The source workflow, configuration contract, output paths, recovery behavior and validation limits are in the [HarnessX guide](../../docs/harnessx.md). The original real proposer run chose two no-ops. A separate hand-authored candidate passed native checks and one fresh benchmark task; that check verifies the modification path without claiming algorithm-generated improvement or paper reproduction.
