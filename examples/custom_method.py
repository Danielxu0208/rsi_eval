"""A user-owned loop: replace candidate creation/selection with your method."""
from pathlib import Path
from rsi_eval import BenchmarkSelection, RunOptions, TargetSpec, evaluate


def evaluate_candidates(candidates, *, model, codex_auth_dir, codex_version, output_dir):
    evolve = BenchmarkSelection("terminal-bench", "2.0", task_ids=("extract-elf",))
    # These choices are local to this method, not categories in the registry.
    test = BenchmarkSelection("swe-bench-verified", "1", task_ids=("astropy__astropy-12907",))
    scored = []
    for index, candidate in enumerate(candidates):
        target = TargetSpec(Path(candidate), "rsi_eval.harbor_codex:Agent", model=model,
                            config={"codex_auth_dir": str(codex_auth_dir), "codex_version": codex_version})
        result = evaluate(target=target, benchmark=evolve, options=RunOptions(),
                          output_dir=Path(output_dir) / f"candidate-{index}")
        if result.status != "completed":
            raise RuntimeError(f"candidate evaluation incomplete: {result.report_path}")
        scored.append((sum(t.reward for t in result.trials), target))
    if not scored:
        raise ValueError("provide at least one candidate directory")
    winner = max(scored, key=lambda item: item[0])[1]
    return evaluate(target=winner, benchmark=test, options=RunOptions(),
                    output_dir=Path(output_dir) / "heldout")
