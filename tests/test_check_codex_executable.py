"""Host-side Codex prerequisite probes must honor explicit executable paths."""
import json
from pathlib import Path
import sys

from rsi_eval import BenchmarkSelection, RunOptions, TargetSpec, check
from rsi_eval.evaluation import CODEX_AGENT, _job_config, preview


def test_check_uses_requested_host_executable_without_changing_container_config(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "agent.py").write_text("# fixture")
    auth = tmp_path / "auth"
    auth.mkdir()
    (auth / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
        "access_token": "fixture-not-a-secret", "refresh_token": "fixture-not-a-secret"}}))
    executable = tmp_path / "dedicated-codex"
    calls = tmp_path / "version-calls.json"
    executable.write_text(f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n"
                          f"Path({str(calls)!r}).write_text(json.dumps(sys.argv[1:]))\n"
                          "assert sys.argv[1:] == ['--version']\nprint('codex-cli 0.162.0-alpha.2')\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", "/no-bare-codex-on-path")
    monkeypatch.setattr("rsi_eval.evaluation._probe", lambda *args, **kwargs: {"ok": True, "detail": "harbor 0.18.0"})
    target = TargetSpec(candidate, CODEX_AGENT,
        {"codex_auth_dir": str(auth), "codex_version": "0.162.0-alpha.2"}, "fixture-model")
    benchmark = BenchmarkSelection("terminal-bench", "2.0", ("extract-elf",))
    result = check(target=target, benchmark=benchmark, options=RunOptions(environment="local"),
                   codex_executable=str(executable))
    assert result["ok"] is True
    assert json.loads(calls.read_text()) == ["--version"]
    resolved = preview(target=target, benchmark=benchmark, output_dir=tmp_path / "run")
    job = _job_config(resolved, tmp_path / "snapshot")
    assert "codex_executable" not in job["agents"][0]["kwargs"]
    assert str(executable) not in json.dumps(job)


def test_ahe_check_forwards_configured_host_executable(tmp_path, monkeypatch):
    import integrations.ahe.launcher as ahe
    import rsi_eval.evaluation as evaluation
    config = {"codex": {"model": "fixture", "auth_dir": str(tmp_path / "auth"),
                        "expected_version": "fixture", "executable": str(tmp_path / "custom-codex")},
              "seed": str(tmp_path), "evolve": {"name": "terminal-bench", "version": "2.0", "task_ids": ["extract-elf"]},
              "run_options": {}}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    monkeypatch.setattr(ahe, "validate", lambda config: None)
    monkeypatch.setattr(ahe, "verify_prepared", lambda path: None)
    monkeypatch.setattr(ahe, "load_upstream", lambda path: None)
    seen = {}
    def fake_check(**kwargs):
        seen.update(kwargs)
        return {"ok": True}
    monkeypatch.setattr(evaluation, "check", fake_check)
    assert ahe.main(["check", "--checkout", str(tmp_path), "--config", str(path)]) == 0
    assert seen["codex_executable"] == str(tmp_path / "custom-codex")
    assert "codex_executable" not in seen["target"].config


def test_rrsi_relative_host_executable_is_frozen_before_candidate_subprocesses(tmp_path):
    from integrations.rrsi.prepare import normalize_config
    config = normalize_config({"codex": {"model": "fixture", "auth_dir": "auth", "expected_version": "fixture",
                                        "executable": "tools/custom-codex"},
        "evolve": {"name": "terminal-bench", "version": "2.0", "task_ids": ["extract-elf"]}}, tmp_path)
    assert config["codex"]["executable"] == str(tmp_path / "tools/custom-codex")
