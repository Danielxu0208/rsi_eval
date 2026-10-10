"""Support records cannot turn adapter availability into an unobserved run."""
from copy import deepcopy
from importlib.resources import files
import json
from pathlib import Path
import subprocess
import sys

import pytest

from rsi_eval import BenchmarkSelection, register_benchmark, support_status
from rsi_eval.benchmarks import resolve
from rsi_eval.cli import main
from rsi_eval.support_status import render_markdown, validate_support_data

ROOT = Path(__file__).resolve().parents[1]


def raw_data():
    return json.loads(files("rsi_eval").joinpath("data/support_status.json").read_text())


def test_every_exact_catalog_and_method_identity_is_reviewed_with_unchanged_receipts():
    data = raw_data()
    validate_support_data(data, evidence_root=ROOT)
    assert len(data["benchmarks"]) == 26
    assert sum(r["lifecycle"] == "active" for r in data["benchmarks"]) == 24


@pytest.mark.parametrize("change", ["missing", "duplicate", "task", "protocol", "method"])
def test_catalog_or_method_drift_requires_new_review(change, monkeypatch):
    from rsi_eval import methods
    data = raw_data()
    catalog = [resolve(BenchmarkSelection(r["name"], r["version"])) for r in data["benchmarks"]]
    for spec in catalog:
        spec.pop("task_ids"); spec.pop("selection")
    if change == "missing":data["benchmarks"].pop()
    elif change == "duplicate":data["benchmarks"].append(deepcopy(data["benchmarks"][0]))
    elif change == "task":catalog[0]["tasks"][0]["ref"] = "sha256:" + "0" * 64
    elif change == "protocol":catalog[0]["protocol"] = {"changed":True}
    elif change == "method":
        monkeypatch.setitem(methods.METHODS["ahe"], "commit", "0" * 40)
    with pytest.raises(ValueError):
        validate_support_data(data, catalog=catalog)


@pytest.mark.parametrize("change", ["task", "receipt", "missing_receipt", "status", "stage"])
def test_unknown_validation_scopes_are_rejected(change):
    data = raw_data()
    o = data["methods"]["meta-harness"]["observations"][0]
    if change == "task":o["task_ids"] = ["missing-task"]
    elif change == "receipt":o["evidence"] = ["missing-receipt"]
    elif change == "missing_receipt":o["evidence"] = []
    elif change == "status":o["status"] = "all_passed"
    elif change == "stage":o["stage"] = "everything"
    with pytest.raises(ValueError):
        validate_support_data(data)


def test_method_filters_keep_binary_policy_distinct_from_raw_continuous_metric():
    record = support_status(benchmark="drop@1.0-agent", method="ahe")
    row, = record["benchmarks"]
    assert set(row["method_compatibility"]) == {"ahe"}
    assert row["reward_domain"] == "unit_interval"
    policy = row["method_compatibility"]["ahe"]
    assert "binary score mapping required" in policy["score_requirement"]
    assert policy["adapter_status"] == "available_subject_to_prerequisites"
    assert not policy["scored_trial_observed"] and not policy["evolve_observed"]
    assert policy["observations"] == []
    assert row["validation"]["target"][0]["task_ids"]


def test_method_final_test_does_not_establish_evolution_on_that_benchmark():
    row, = support_status(benchmark="swe-bench-verified@1", method="rrsi")["benchmarks"]
    method = row["method_compatibility"]["rrsi"]
    assert method["scored_trial_observed"]
    assert not method["evolve_observed"]
    assert method["observations"][0]["task_ids"] == ["astropy__astropy-12907"]


@pytest.mark.parametrize("status", ["blocked", "unavailable", "not_recorded"])
def test_method_stage_without_observed_status_does_not_claim_execution(status, monkeypatch, tmp_path):
    import importlib
    module = importlib.import_module("rsi_eval.support_status")
    data = raw_data()
    data["methods"]["meta-harness"]["observations"][0]["status"] = status
    catalog = module._bundled_catalog()
    monkeypatch.setattr(module, "_bundled_catalog", lambda: catalog)
    (tmp_path / "data").mkdir()
    (tmp_path / "data/support_status.json").write_text(json.dumps(data))
    monkeypatch.setattr(module, "files", lambda package: tmp_path)
    row, = support_status(benchmark="terminal-bench@2.0", method="meta-harness")["benchmarks"]
    method = row["method_compatibility"]["meta-harness"]
    assert not method["evolve_observed"] and not method["scored_trial_observed"]
    assert f"{status} evolve" in module.format_status(support_status(benchmark="terminal-bench@2.0", method="meta-harness"))


def test_live_image_smoke_is_method_capability_evidence_without_benchmark_credit():
    data = support_status(benchmark="drop", method="harnessx")
    method = data["methods"]["harnessx"]
    assert "PNG, JPEG and WebP" in method["input_modalities"]["image"]
    assert method["input_modalities"]["audio"].startswith("Unsupported")
    assert any("200,000" in limit for limit in method["execution_limits"])
    capability, = method["capability_validation"]
    assert capability["status"] == "observed" and "zero benchmark calls" in capability["scope"]
    row, = data["benchmarks"]
    assert not row["method_compatibility"]["harnessx"]["scored_trial_observed"]
    malformed = raw_data()
    malformed["methods"]["harnessx"]["capability_validation"][0]["benchmark"] = "drop"
    with pytest.raises(ValueError, match="separate"):
        validate_support_data(malformed)


def test_apex_world_checks_and_tool_route_are_not_paid_grading_or_method_execution():
    row, = support_status(benchmark="apex-agents@1.1.1", method="harnessx")["benchmarks"]
    assert row["task_count"] == 240
    assert len(row["validation"]["environment"][0]["task_ids"]) == 31
    assert row["validation"]["target"][0]["status"] == "blocked"
    method = row["method_compatibility"]["harnessx"]
    assert not method["scored_trial_observed"] and not method["evolve_observed"]
    assert method["observations"][0]["stage"] == "tool_route_only"


def test_gpu_and_published_reference_limits_remain_visible():
    row, = support_status(benchmark="frontier-eng@v1-bafa7d5")["benchmarks"]
    assert len(row["validation"]["environment"][-1]["task_ids"]) == 44
    assert any("NVIDIA" in item for item in row["prerequisites"])
    for benchmark in ["jobbench", "harvey-lab"]:
        row, = support_status(benchmark=benchmark)["benchmarks"]
        assert row["validation"]["reference"][0]["status"] == "unavailable"
        assert row["validation"]["target"][0]["status"] == "blocked"


def test_family_query_retains_exact_versions_and_legacy_scope():
    rows = support_status(benchmark="engdesign-open")["benchmarks"]
    assert {(r["version"],r["task_count"],r["lifecycle"]) for r in rows} == {
        ("open-subset-c8ec338-v1",2,"legacy-subset"), ("open-c8ec338-v1",67,"active")}


@pytest.mark.parametrize("kwargs", [{"benchmark":"missing"},{"benchmark":"gpqa@missing"},{"method":"tthe"}])
def test_unknown_filter_is_explicit(kwargs):
    with pytest.raises(ValueError, match="Unknown"):
        support_status(**kwargs)


def test_custom_registration_is_unreviewed_and_does_not_inherit_another_version(monkeypatch):
    from rsi_eval import benchmarks
    monkeypatch.setattr(benchmarks, "_EXTRA", {})
    register_benchmark({"name":"terminal-bench", "version":"custom", "dataset_ref":"custom-source",
                        "tasks":[{"id":"x", "name":"x", "ref":"sha256:"+"0"*64}]})
    row, = support_status(benchmark="terminal-bench@custom")["benchmarks"]
    assert row["review_status"] == "unreviewed_custom_registration"
    assert all(m["adapter_status"] == "unreviewed" and not m["observations"]
               for m in row["method_compatibility"].values())


def test_cli_and_sdk_share_identical_filtering_and_receipts(capsys):
    args = {"benchmark":"terminal-bench@2.0","method":"rrsi"}
    assert main(["status","--json","--benchmark",args["benchmark"],"--method",args["method"]]) == 0
    assert json.loads(capsys.readouterr().out) == support_status(**args)
    assert main(["status","--benchmark","missing@1"]) == 2
    assert "Unknown benchmark" in capsys.readouterr().err


def test_plain_cli_displays_mapping_prerequisite_and_unverified_pair(capsys):
    assert main(["status","--benchmark","harvey-lab","--method","ahe"]) == 0
    output = capsys.readouterr().out
    assert "binary score mapping required" in output
    assert "Anthropic and OpenAI" in output
    assert "no recorded method/benchmark execution" in output
    assert "blocked" in output


def test_support_view_is_offline_without_optional_dependencies(tmp_path):
    code = '''
import sys
sys.path.insert(0, SOURCE)
class DenyOptional:
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'harbor', 'pydantic', 'yaml', 'jsonschema', 'litellm'}:
            raise AssertionError('Optional dependency imported: ' + fullname)
sys.meta_path.insert(0,DenyOptional())
import subprocess
subprocess.run = lambda *a,**k: (_ for _ in ()).throw(AssertionError('subprocess started'))
from rsi_eval import support_status
assert len(support_status()['benchmarks']) == 26
'''.replace('SOURCE', repr(str(ROOT / 'src')))
    subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, check=True, capture_output=True)


def test_generated_document_and_update_check_match_packaged_data():
    assert (ROOT / "docs/support-status.md").read_text() == render_markdown(support_status())
    assert "run.." not in render_markdown(support_status())
    subprocess.run([sys.executable, "scripts/update_support_status.py", "--check"], cwd=ROOT, check=True)
