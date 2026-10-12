"""Compatibility queries preserve requirements without a historical run index."""
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
from rsi_eval.support_status import format_status, render_markdown, validate_support_data

ROOT = Path(__file__).resolve().parents[1]


def raw_data():
    return json.loads(files("rsi_eval").joinpath("data/support_status.json").read_text())


def test_every_exact_catalog_and_method_identity_has_current_declarations():
    data = raw_data()
    validate_support_data(data)
    assert data["schema_version"] == 2
    assert len(data["benchmarks"]) == 26
    assert sum(r["lifecycle"] == "active" for r in data["benchmarks"]) == 24


@pytest.mark.parametrize("change", ["missing", "duplicate", "task", "protocol", "method"])
def test_catalog_or_method_drift_requires_updated_declarations(change, monkeypatch):
    from rsi_eval import methods
    data = raw_data()
    catalog = [resolve(BenchmarkSelection(r["name"], r["version"])) for r in data["benchmarks"]]
    for spec in catalog:
        spec.pop("task_ids")
        spec.pop("selection")
    if change == "missing":
        data["benchmarks"].pop()
    elif change == "duplicate":
        data["benchmarks"].append(deepcopy(data["benchmarks"][0]))
    elif change == "task":
        catalog[0]["tasks"][0]["ref"] = "sha256:" + "0" * 64
    elif change == "protocol":
        catalog[0]["protocol"] = {"changed": True}
    elif change == "method":
        monkeypatch.setitem(methods.METHODS["ahe"], "commit", "0" * 40)
    with pytest.raises(ValueError):
        validate_support_data(data, catalog=catalog)


@pytest.mark.parametrize("change", ["schema", "domain", "score_rules", "modalities"])
def test_invalid_compatibility_declarations_are_rejected(change):
    data = raw_data()
    if change == "schema":
        data["schema_version"] = 0
    elif change == "domain":
        data["benchmarks"][0]["reward_domain"] = "invented-score"
    elif change == "score_rules":
        data["methods"]["ahe"]["score_rules"].pop("unit_interval")
    elif change == "modalities":
        data["methods"]["harnessx"]["input_modalities"].pop("audio")
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
    assert "Codex wrapper through Harbor" in policy["execution_route"]
    assert set(record["methods"]) == {"ahe"}


def test_status_exposes_configuration_without_historical_run_records():
    data = support_status()
    excluded = {"reviewed_on", "evidence", "validation", "observations", "capability_validation",
                "evolve_observed", "scored_trial_observed"}
    def inspect(value):
        if isinstance(value, dict):
            assert not set(value) & excluded
            for item in value.values():
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)
    inspect(data)
    assert all(row["registration_source"] == "bundled" for row in data["benchmarks"])


def test_harnessx_declares_supported_image_formats_and_actual_media_limits():
    method = support_status(method="harnessx")["methods"]["harnessx"]
    assert "PNG, JPEG and WebP" in method["input_modalities"]["image"]
    assert method["input_modalities"]["audio"].startswith("Unsupported")
    assert method["input_modalities"]["video"].startswith("Unsupported")
    assert any("200,000" in limit for limit in method["execution_limits"])
    assert any("GIF and animated PNG/WebP" in limit for limit in method["execution_limits"])


def test_gpu_judge_and_reference_requirements_remain_visible():
    row, = support_status(benchmark="frontier-eng@v1-bafa7d5")["benchmarks"]
    assert row["task_count"] == 48
    assert any("NVIDIA/CUDA" in item for item in row["prerequisites"])
    for benchmark in ("jobbench", "harvey-lab", "apex-agents"):
        row, = support_status(benchmark=benchmark)["benchmarks"]
        assert any("judge credentials" in item for item in row["prerequisites"])
        if benchmark != "apex-agents":
            assert any("oracle is unavailable" in item for item in row["limitations"])


def test_family_query_retains_exact_versions_and_legacy_scope():
    rows = support_status(benchmark="engdesign-open")["benchmarks"]
    assert {(r["version"], r["task_count"], r["lifecycle"]) for r in rows} == {
        ("open-subset-c8ec338-v1", 2, "legacy-subset"), ("open-c8ec338-v1", 67, "active")}


@pytest.mark.parametrize("kwargs", [{"benchmark": "missing"}, {"benchmark": "gpqa@missing"}, {"method": "tthe"}])
def test_unknown_filter_is_explicit(kwargs):
    with pytest.raises(ValueError, match="Unknown"):
        support_status(**kwargs)


def test_custom_registration_keeps_its_identity_and_requires_explicit_objective(monkeypatch):
    from rsi_eval import benchmarks
    monkeypatch.setattr(benchmarks, "_EXTRA", {})
    register_benchmark({"name": "terminal-bench", "version": "custom", "dataset_ref": "custom-source",
                        "tasks": [{"id": "x", "name": "x", "ref": "sha256:" + "0" * 64}]})
    row, = support_status(benchmark="terminal-bench@custom")["benchmarks"]
    assert row["registration_source"] == "process_local"
    assert row["dataset_ref"] == "custom-source"
    assert row["reward_domain"] == "unknown"
    assert all("Declare" in m["score_requirement"] for m in row["method_compatibility"].values())


def test_cli_and_sdk_share_identical_filtering(capsys):
    args = {"benchmark": "terminal-bench@2.0", "method": "rrsi"}
    assert main(["status", "--json", "--benchmark", args["benchmark"], "--method", args["method"]]) == 0
    assert json.loads(capsys.readouterr().out) == support_status(**args)
    assert main(["status", "--benchmark", "missing@1"]) == 2
    assert "Unknown benchmark" in capsys.readouterr().err


def test_plain_cli_displays_objective_and_prerequisite(capsys):
    assert main(["status", "--benchmark", "harvey-lab", "--method", "ahe"]) == 0
    output = capsys.readouterr().out
    assert "binary score mapping required" in output
    assert "Anthropic and OpenAI" in output
    assert "configured runtime" in output
    assert "blocked" not in output


def test_overview_shows_configurations_and_reward_domains():
    output = format_status(support_status(), detailed=False)
    assert "terminal-bench@2.0 | 89 | active | binary" in output
    assert "frontier-eng@v1-bafa7d5 | 48 | active | heterogeneous" in output
    assert "prerequisites and objective rules" in output


def test_support_view_is_offline_without_optional_dependencies(tmp_path):
    code = """
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
""".replace("SOURCE", repr(str(ROOT / "src")))
    subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, check=True, capture_output=True)


def test_generated_document_and_update_check_match_packaged_data():
    document = render_markdown(support_status())
    assert (ROOT / "docs/support-status.md").read_text() == document
    assert "validation-" not in document
    subprocess.run([sys.executable, "scripts/update_support_status.py", "--check"], cwd=ROOT, check=True)
