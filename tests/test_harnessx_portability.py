"""Optional native HarnessX resume/test after a receipt-checked relocation."""
import asyncio
import json
from pathlib import Path
import re
import shutil

import pytest

from test_harnessx_native import native_checkout, strict_test_response
from test_portability import portable_record
from rsi_eval import EvaluationResult, TrialResult, method_test, portability
from rsi_eval.artifacts import artifact_hash, snapshot
from rsi_eval.codex import CodexResult
from integrations.harnessx import launcher


@pytest.mark.parametrize("interrupt", [False, True])
def test_native_harnessx_export_import_resume_and_independent_test(native_checkout, tmp_path, monkeypatch, interrupt):
    from harnessx.meta_harness.agent import MetaAgent
    from harnessx.meta_harness.journal import JournalEntrySpec, append_entry

    root = tmp_path / "original"
    root.mkdir()
    config = {"checkout": str(native_checkout), "output_dir": str(root / "run"), "rounds": 2,
              "codex": {"model": "test", "expected_version": "test", "auth_dir": str(tmp_path / "auth")},
              "evolve": {"name": "terminal-bench", "version": "2.0", "task_ids": ["extract-elf"]},
              "test": [], "run_options": {"concurrency": 1}, "method": {"max_steps": 12, "timeout_seconds": 30}}
    (root / "configuration.json").write_text(json.dumps(config))
    config = launcher.load_experiment(root / "configuration.json")
    active = {"round": 0, "phase": 0, "stopped": False}
    native_evolve = MetaAgent.evolve
    prompts, evaluations = [], []

    async def observed_evolve(self, **kwargs):
        if interrupt and active["round"] == 1 and not active["stopped"]:
            active["stopped"] = True
            raise KeyboardInterrupt("deterministic interruption before second native round")
        active.update(round=active["round"] + 1, phase=0, output=kwargs["output_dir"],
                      current=kwargs["current_config"], memo=self.memo_path,
                      feedback=kwargs["trajectories_dir"])
        return await native_evolve(self, **kwargs)

    def fake_codex(prompt, **kwargs):
        prompts.append(prompt)
        response = {"content": "OK", "tool_calls": []}
        if "Synthetic replay smoke check. Reply with exactly: OK" not in prompt:
            phase, out, index = active["phase"], active["output"], active["round"]
            def tool(name, arguments):
                return {"content": "Use the native tool", "tool_calls": [{"name": name, "arguments": arguments}]}
            if phase == 0:
                response = tool("Read", {"file_path": str(out / "_meta_scratch/TASK.md")})
            elif phase == 1:
                path = (next(active["feedback"].rglob("trajectory.md")) if index == 1
                        else out / "_meta_scratch/CONTEXT.md")
                response = tool("Read", {"file_path": str(path)})
            elif phase == 2:
                text = re.sub(r"threshold: \d+", f"threshold: {7 + index}", active["current"].read_text(), count=1)
                response = tool("Write", {"file_path": str(out / "config.yaml"), "content": text})
            elif phase == 3:
                response = tool("Write", {"file_path": str(out / "_meta_scratch/candidates.md"),
                    "content": f"## Candidate C-{index:03d}\n[lens: failure | lever: control | intent: corrective]\nRepeated edits in trajectory.md motivate a threshold change."})
            elif phase == 4:
                scratch = tmp_path / "fixture-journal.md"
                if active["memo"].exists():
                    scratch.write_text(active["memo"].read_text())
                else:
                    scratch.unlink(missing_ok=True)
                append_entry(scratch, JournalEntrySpec(round=index, label=f"Edit threshold {index}",
                    hypothesis_id=f"edit-threshold-{index}", levers=["control"], predicted_affected=["extract-elf"],
                    prose="Read prior feedback and adjust repeated-edit warning.",
                    extra_frontmatter={"cited_candidates": [f"C-{index:03d}"]}))
                response = tool("Write", {"file_path": str(active["memo"]), "content": scratch.read_text()})
            active["phase"] += 1
        return CodexResult(text=strict_test_response(response, kwargs["output_schema"]), events=[],
                           usage={"input_tokens": 10, "output_tokens": 2}, cli_version="test")

    def fake_evaluate(*, target, benchmark, options, output_dir, **kwargs):
        output_dir.mkdir(parents=True)
        snapshot(target.artifact_dir, output_dir / "candidate")
        digest = artifact_hash(target.artifact_dir)
        native = output_dir / "native/trial"
        (native / "agent").mkdir(parents=True)
        (native / "agent/trajectory.md").write_text("Task failed after repeated edits; reward=0; inspect edit threshold.")
        resolved = {"target": {"sha256": digest}, "output_dir": str(output_dir)}
        (output_dir / "resolved.json").write_text(json.dumps(resolved))
        result = EvaluationResult("completed", [TrialResult("extract-elf", 0, "completed", 0,
            input_tokens=10, output_tokens=2, artifact_dir=str(native))], {}, {}, str(native), resolved,
            report_path=str(output_dir / "result.json"))
        (output_dir / "result.json").write_text(json.dumps(result.to_dict()))
        evaluations.append(str(output_dir))
        return result

    monkeypatch.setattr(MetaAgent, "evolve", observed_evolve)
    monkeypatch.setattr("integrations.harnessx.provider.run_codex", fake_codex)
    monkeypatch.setattr(launcher, "evaluate", fake_evaluate)
    if interrupt:
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(launcher.run(config))
    else:
        asyncio.run(launcher.run(config))
    state = json.loads((root / "run/result.json").read_text())
    selected = Path(state["current_candidate"])
    selected_relative, selected_hash = selected.relative_to(root), artifact_hash(selected)
    raw = Path(state["current_report"])
    raw_relative, raw_bytes = raw.relative_to(root), raw.read_bytes()
    record = portable_record(root, "harnessx", config, monkeypatch)
    bundle, destination = tmp_path / "bundle", tmp_path / "relocated"
    portability.export_experiment(root, bundle)
    portability.import_experiment(bundle, destination, Path(record["environment"]), tmp_path / "new-auth")
    shutil.rmtree(root)
    assert artifact_hash(destination / selected_relative) == selected_hash
    assert (destination / raw_relative).read_bytes() == raw_bytes
    moved_config = launcher.load_experiment(destination / "configuration.json")
    resumed = asyncio.run(launcher.run(moved_config, resume=True))
    assert resumed["status"] == "completed" and len(resumed["rounds"]) == 2
    assert all(row["structural_changes"] for row in resumed["rounds"])
    if interrupt:
        assert resumed["rounds"][1]["history_reads"]
        assert "Journal Context for R2" in "\n".join(prompts)
        assert evaluations[-1].startswith(str(destination))

    # Keep host prerequisite probes deterministic, as with the evaluator above.
    auth_checks, version_checks, environment_checks = [], [], []

    def fake_validate_auth(auth_dir):
        auth_checks.append(Path(auth_dir))
        return Path(auth_dir) / "auth.json"

    def fake_codex_version(settings):
        version_checks.append((settings.auth_dir, settings.expected_version))
        return "test"

    def fake_check(**kwargs):
        environment_checks.append(kwargs)
        return {"ok": True, "checks": {"fixture": {"ok": True}}}

    monkeypatch.setattr("rsi_eval.codex.validate_auth", fake_validate_auth)
    monkeypatch.setattr("rsi_eval.codex.codex_version", fake_codex_version)
    method_record = json.loads((destination / "method.json").read_text())
    final = method_test.test_selected(method_record, tmp_path / "additional-test",
        selections=[{"name": "terminal-bench", "version": "2.1", "task_ids": ["extract-elf"]}],
        evaluator=fake_evaluate, checker=fake_check)
    assert final["status"] == "completed", final.get("preflight")
    assert auth_checks == [tmp_path / "new-auth"]
    assert version_checks == [(tmp_path / "new-auth", "test")]
    assert len(environment_checks) == 1
    checked = environment_checks[0]
    assert checked["target"].artifact_dir == tmp_path / "additional-test/candidate"
    assert checked["target"].config["codex_auth_dir"] == str(tmp_path / "new-auth")
    assert checked["benchmark"].version == "2.1"
    assert final["candidate_sha256"] == artifact_hash(Path(resumed["current_candidate"]))
