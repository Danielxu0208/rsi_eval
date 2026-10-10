"""Offline integration checks; the pinned native fixtures need no checkout."""
import asyncio
from dataclasses import dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

from integrations.harnessx.bundle import freeze_config
from integrations.harnessx.launcher import load_experiment, feedback, target_for
from integrations.harnessx.prepare import PATCHES
from integrations.harnessx.provider import CodexProvider
from rsi_eval.codex import CodexAuthError, CodexQuotaError, CodexResult, CodexSettings
from rsi_eval import EvaluationResult, TrialResult


FIXTURE = Path(__file__).parent / "fixtures" / "harnessx_native"


@pytest.fixture
def native(monkeypatch):
    """Load original orchestration modules; external model/runtime seams are test doubles."""
    for name in ("_hx_fixture", "_hx_fixture.meta_harness", "_hx_fixture.core", "_hx_fixture.tracing"):
        module = ModuleType(name)
        module.__path__ = [str(FIXTURE)]
        monkeypatch.setitem(sys.modules, name, module)
    modules = {}
    for short in ("replay", "journal", "validate_workflow", "agent"):
        name = "_hx_fixture.meta_harness." + short
        spec = importlib.util.spec_from_file_location(name, FIXTURE / f"{short}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        modules[short] = module
    return SimpleNamespace(**modules)


def test_native_fixture_hashes():
    manifest = json.loads((FIXTURE / "manifest.json").read_text())
    from integrations.harnessx import UPSTREAM_COMMIT
    assert manifest["commit"] == UPSTREAM_COMMIT
    for name, row in manifest["files"].items():
        assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == row["sha256"]


def test_native_evidence_rejects_absent_candidates(native, tmp_path):
    out = tmp_path / "round"
    scratch = out / "_meta_scratch"
    scratch.mkdir(parents=True)
    reports = []
    validator = native.validate_workflow.EvolveValidator(inner_model=object())
    with pytest.raises(RuntimeError, match="evidence gate"):
        validator._evidence(scratch, out / "config.yaml", {"processors_added": ["P"]}, reports)
    assert reports[-1].ok is False


def test_native_evidence_checks_journal_citations(native, tmp_path):
    scratch = tmp_path / "_meta_scratch"
    scratch.mkdir()
    (scratch / "candidates.md").write_text("## Candidate C-001\nEvidence from actual trajectory")
    journal = tmp_path / "learnings.md"
    native.journal.append_entry(journal, native.journal.JournalEntrySpec(
        round=1, label="test", hypothesis_id="test", levers=["control"],
        predicted_affected=["task"], prose="Change explanation",
        extra_frontmatter={"cited_candidates": ["C-999"]}))
    validator = native.validate_workflow.EvolveValidator(inner_model=object(), memo_path=journal)
    with pytest.raises(RuntimeError, match="evidence gate"):
        validator._evidence(scratch, tmp_path / "config.yaml", {"processors_added": ["P"]}, [])


def test_native_journal_backfill_and_next_round_context(native, tmp_path):
    journal = tmp_path / "learnings.md"
    native.journal.append_entry(journal, native.journal.JournalEntrySpec(
        round=1, label="Inspect errors", hypothesis_id="inspect-errors", levers=["instruction"],
        predicted_affected=["task"], prose="The preceding trajectory contained an unchecked error.",
        extra_frontmatter={"cited_candidates": ["C-001"]}))
    assert native.journal.fill_gating(journal, 1, "accepted", {"task": "flipped"})
    meta = native.agent.MetaAgent(inner_model=object(), memo_path=journal, max_cost_usd=None)
    scratch = tmp_path / "round2" / "_meta_scratch"
    scratch.mkdir(parents=True)
    brief, context = meta._prepare_brief_and_context(
        current_config_path=tmp_path / "candidate.yaml", trajectories_dir=tmp_path / "feedback",
        output_dir=scratch.parent, scratch_dir=scratch)
    assert context.is_file()
    assert "Inspect errors" in context.read_text()
    assert "journal_context" in brief.read_text()


def test_native_meta_agent_calls_postflight_and_rejects_missing_output(native, monkeypatch, tmp_path):
    task_module = ModuleType("_hx_fixture.core.harness")
    task_module.BaseTask = lambda **kw: SimpleNamespace(**kw)
    monkeypatch.setitem(sys.modules, task_module.__name__, task_module)
    tracer_module = ModuleType("_hx_fixture.tracing.journal")
    tracer_module.HarnessJournal = lambda **kw: SimpleNamespace(**kw)
    monkeypatch.setitem(sys.modules, tracer_module.__name__, tracer_module)
    monkeypatch.setattr(native.agent, "build_meta_agent_harness_config", lambda **kw: kw)
    called = []

    class Model:
        def agentic(self, config):
            async def run(task):
                called.append("model")
            return SimpleNamespace(run=run)

    trajectories = tmp_path / "feedback"
    trajectories.mkdir()
    current = tmp_path / "current.yaml"
    current.write_text("processors: []")
    meta = native.agent.MetaAgent(inner_model=Model(), max_cost_usd=None)
    with pytest.raises(RuntimeError, match="did not produce"):
        asyncio.run(meta.evolve(current_config=current, trajectories_dir=trajectories,
                                output_dir=tmp_path / "round"))
    assert called == ["model"]


def test_declared_patch_repairs_constructor_and_only_changes_smoke_timeout():
    source = (FIXTURE / "agent.py").read_text()
    for before, after in PATCHES["harnessx/meta_harness/agent.py"]:
        assert source.count(before) == 1
        source = source.replace(before, after)
    assert "replay_timeout_s=replay_timeout_s," in source
    assert "await validator.run(" in source
    assert PATCHES["recipe/tb2_evolver/run.py"][0][0].strip() == "require_evidence=not args.no_require_evidence,"


def test_freeze_copies_template_and_remains_independent(tmp_path):
    source = tmp_path / "source"
    (source / "templates").mkdir(parents=True)
    template = source / "templates" / "system.j2"
    template.write_text("original prompt")
    config = source / "config.yaml"
    config.write_text(f"processors:\n- template_path: {template}\n")
    frozen = freeze_config(config, tmp_path / "candidate")
    template.write_text("changed upstream prompt")
    import yaml
    path = Path(yaml.safe_load(frozen.read_text())["processors"][0]["template_path"])
    assert path.read_text() == "original prompt"
    assert path.is_relative_to(tmp_path / "candidate")


def test_freeze_rejects_recursive_dependency_copy(tmp_path):
    (tmp_path / "template.j2").write_text("test")
    source = tmp_path / "config.yaml"
    source.write_text(f"template_path: {tmp_path / 'template.j2'}\n")
    with pytest.raises(ValueError, match="outside dependency"):
        freeze_config(source, tmp_path / "candidate")


def test_config_normalizes_paths_and_arbitrary_selection(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"checkout": "source", "output_dir": "run",
        "codex": {"model": "model", "expected_version": "v", "auth_dir": "auth"},
        "evolve": {"name": "drop", "version": "1.0-agent", "task_ids": ["x"]},
        "run_options": {"harbor_executable": "venv/bin/harbor"}}))
    config = load_experiment(path)
    assert config["evolve"]["name"] == "drop"
    assert config["codex"]["auth_dir"] == str(tmp_path / "auth")
    (tmp_path / "candidate").mkdir()
    (tmp_path / "candidate" / "config.yaml").write_text("processors: []\n")
    target = target_for(config, tmp_path / "candidate")
    assert target.config["artifact_dir"] == str(target.artifact_dir)
    assert target.entrypoint == "integrations.harnessx.harbor_agent:Agent"


def fake_events_module(monkeypatch):
    package = ModuleType("harnessx")
    package.__path__ = []
    core = ModuleType("harnessx.core")
    core.__path__ = []
    events = ModuleType("harnessx.core.events")
    for name in ("Usage", "ModelResponseEvent", "ToolCall"):
        setattr(events, name, lambda **kwargs: SimpleNamespace(**kwargs))
    for module in (package, core, events):
        monkeypatch.setitem(sys.modules, module.__name__, module)


@dataclass
class Message:
    role: str
    content: str


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict


def test_provider_returns_intents_and_never_executes_tools(monkeypatch, tmp_path):
    fake_events_module(monkeypatch)
    seen = []
    def fake(prompt, **kwargs):
        seen.append(kwargs)
        return CodexResult(text=json.dumps({"content": "", "tool_calls": [
            {"name": "Bash", "arguments": {"command": "echo native"}}]}), events=[],
            usage={"input_tokens": 12, "output_tokens": 3, "cached_input_tokens": None}, cli_version="v")
    monkeypatch.setattr("integrations.harnessx.provider.run_codex", fake)
    provider = CodexProvider(CodexSettings("m", tmp_path / "auth"), tmp_path / "calls")
    event = asyncio.run(provider.complete([Message("user", "task")], [Tool("Bash", "shell", {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"], "additionalProperties": False})]))
    assert event.tool_calls[0].input == {"command": "echo native"}
    assert seen[0]["mode"] == "role"
    assert seen[0]["output_schema"]
    assert provider.accounting()["cached_input_tokens"] is None
    assert provider.accounting()["cost_usd"] is None


def test_provider_failed_call_accounting(monkeypatch, tmp_path):
    fake_events_module(monkeypatch)
    def fake(prompt, **kwargs):
        (kwargs["log_dir"] / "result.json").write_text(json.dumps({
            "usage": {"input_tokens": 8, "output_tokens": None}, "codex_cli_calls": 1,
            "status": "failed", "error_type": "CodexAuthError"}))
        raise CodexAuthError("login expired")
    monkeypatch.setattr("integrations.harnessx.provider.run_codex", fake)
    provider = CodexProvider(CodexSettings("m", tmp_path / "auth"), tmp_path / "calls")
    with pytest.raises(CodexAuthError):
        asyncio.run(provider.complete([Message("user", "task")], []))
    assert provider.accounting()["codex_cli_calls"] == 1
    assert provider.accounting()["input_tokens"] == 8
    assert provider.accounting()["output_tokens"] is None


@pytest.mark.parametrize("failure", [CodexAuthError, CodexQuotaError])
def test_provider_auth_quota_latch_prevents_native_retry(monkeypatch, tmp_path, failure):
    fake_events_module(monkeypatch)
    calls = []
    def fake(prompt, **kwargs):
        calls.append(prompt)
        raise failure("stop")
    monkeypatch.setattr("integrations.harnessx.provider.run_codex", fake)
    provider = CodexProvider(CodexSettings("m", tmp_path / "auth"), tmp_path / "calls")
    for _ in range(3):
        with pytest.raises(failure):
            asyncio.run(provider.complete([Message("user", "task")], []))
    assert len(calls) == 1


def test_cancelled_provider_waits_for_cli_accounting(monkeypatch, tmp_path):
    fake_events_module(monkeypatch)
    started, release = threading.Event(), threading.Event()
    def fake(prompt, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        (kwargs["log_dir"] / "result.json").write_text(json.dumps({
            "usage": {"input_tokens": 17, "output_tokens": 3}, "codex_cli_calls": 1,
            "status": "completed"}))
        return CodexResult(text='{"content":"OK","tool_calls":[]}', events=[],
                           usage={"input_tokens": 17, "output_tokens": 3}, cli_version="v")
    monkeypatch.setattr("integrations.harnessx.provider.run_codex", fake)
    provider = CodexProvider(CodexSettings("m", tmp_path / "auth"), tmp_path / "calls")
    async def exercise():
        task = asyncio.create_task(provider.complete([Message("user", "task")], []))
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(exercise())
    assert provider.accounting()["codex_cli_calls"] == 1
    assert provider.accounting()["input_tokens"] == 17


@pytest.mark.parametrize("directory_link", [False, True])
def test_bundle_rejects_symlink_dependency(monkeypatch, tmp_path, directory_link):
    source = tmp_path / "source"
    source.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "prompt.j2").write_text("external")
    linked = source / "linked"
    linked.symlink_to(external if directory_link else external / "prompt.j2", target_is_directory=directory_link)
    reference = linked / "prompt.j2" if directory_link else linked
    config = source / "config.yaml"
    config.write_text(f"template_path: {reference}\n")
    with pytest.raises(ValueError, match="symlinks"):
        freeze_config(config, tmp_path / "candidate")


def test_bundle_copies_only_references_and_rejects_ambient_import(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "system.j2").write_text("prompt")
    (source / "auth.json").write_text("must not be included")
    (source / "unrelated.py").write_text("not part of candidate")
    config = source / "config.yaml"
    config.write_text(f"template_path: {source / 'system.j2'}\n")
    freeze_config(config, tmp_path / "candidate")
    assert not list((tmp_path / "candidate").rglob("auth.json"))
    assert not list((tmp_path / "candidate").rglob("unrelated.py"))
    config.write_text("processors:\n- _target_: ambient_package.Processor\n")
    with pytest.raises(ValueError, match="ambient import"):
        freeze_config(config, tmp_path / "candidate2")


def test_bundle_rejects_outside_file_without_explicit_root(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "template.j2").write_text("prompt")
    config = source / "config.yaml"
    config.write_text(f"template_path: {external / 'template.j2'}\n")
    with pytest.raises(ValueError, match="outside declared roots"):
        freeze_config(config, tmp_path / "candidate")
    freeze_config(config, tmp_path / "candidate2", allowed_roots=[external])


def test_checkout_verifies_staged_tracked_changes(monkeypatch, tmp_path):
    import integrations.harnessx.prepare as prepare
    from integrations.harnessx import UPSTREAM_COMMIT
    calls = []
    def fake_git(root, *args):
        calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return UPSTREAM_COMMIT
        if args == ("diff", "HEAD", "--name-only"):
            return "staged-unapproved.py"
        if args == ("diff", "--name-only"):
            return ""
        raise AssertionError(args)
    monkeypatch.setattr(prepare, "git", fake_git)
    monkeypatch.setattr(prepare, "patched_files", lambda root: {})
    monkeypatch.setattr(prepare, "PATCHES", {})
    with pytest.raises(ValueError, match="Unexpected tracked"):
        prepare.check_checkout(tmp_path)
    assert ("diff", "HEAD", "--name-only") in calls


@pytest.mark.parametrize("status,failure", [("auth_error", CodexAuthError), ("quota_exceeded", CodexQuotaError)])
def test_target_evaluation_preserves_auth_quota_status(monkeypatch, tmp_path, status, failure):
    from integrations.harnessx.launcher import evaluate_candidate
    result = EvaluationResult(status=status, trials=[], metrics={}, usage={}, artifact_dir=str(tmp_path),
                              resolved={}, report_path="report.json")
    monkeypatch.setattr("integrations.harnessx.launcher.evaluate", lambda **kwargs: result)
    monkeypatch.setattr("integrations.harnessx.launcher.target_for", lambda *args: object())
    with pytest.raises(failure):
        evaluate_candidate({"run_options": {}}, tmp_path, {"name": "terminal-bench", "version": "2.0"}, tmp_path)


@pytest.mark.parametrize("kind", ["cat", "echo-path", "cat-with-no-content", "blocked-cat"])
def test_history_audit_requires_bash_contents_not_just_path(tmp_path, kind):
    from integrations.harnessx.launcher import history_reads
    output = tmp_path / "R2"
    context = output / "_meta_scratch" / "CONTEXT.md"
    context.parent.mkdir(parents=True)
    contents = "# Journal Context for R2\n\nPrevious hypothesis: inspect tool errors before changing strategy.\n"
    context.write_text(contents)
    session = output / "meta_workspace" / "sessions" / "run"
    session.mkdir(parents=True)
    command = f"echo {context}" if kind == "echo-path" else f"cat {context}"
    returned = contents if kind in {"cat", "blocked-cat"} else str(context)
    rows = [
        {"type": "raw_assistant", "message": {"tool_calls": [
            {"id": "read1", "name": "Bash", "input": {"command": command}}]}},
        {"event_type": "tool_call", "tool_call_id": "read1", "approved": kind != "blocked-cat"},
        {"type": "raw_tool", "message": {"tool_call_id": "read1", "content": returned}},
    ]
    (session / "events.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    evidence = history_reads(output, tmp_path / "learnings.md")
    assert bool(evidence) == (kind == "cat")
    if evidence:
        assert evidence[0]["evidence"] == "approved_bash_content_match"
        assert evidence[0]["audit_version"] == 2


def test_feedback_copies_agent_trace_and_actual_score(tmp_path):
    trial_dir = tmp_path / "trial"
    (trial_dir / "agent").mkdir(parents=True)
    (trial_dir / "agent" / "trajectory.md").write_text("native tool execution")
    (trial_dir / "verifier").mkdir()
    (trial_dir / "verifier" / "secret.txt").write_text("not copied")
    result = EvaluationResult(status="completed", trials=[TrialResult(
        task_id="task", repetition=0, status="completed", reward=0.5, artifact_dir=str(trial_dir))],
        metrics={}, usage={}, artifact_dir=str(tmp_path), resolved={})
    output = feedback(result, tmp_path / "feedback")
    assert "reward: 0.5" in (output / "task" / "0" / "trajectory.md").read_text()
    assert not list(output.rglob("secret.txt"))


BASH_SCHEMA = {"type": "object", "properties": {"command": {"type": "string"},
               "timeout": {"type": "integer", "default": 120}}, "required": ["command"], "additionalProperties": False}


def test_structured_tool_roundtrip_quotes_newlines_and_optional_defaults(monkeypatch, tmp_path):
    fake_events_module(monkeypatch)
    from integrations.harnessx.provider import response_schema
    from jsonschema import Draft202012Validator
    command = 'cat <<\'EOF\' > config.yaml\nexpected_global_gain: "No flips claimed."\nEOF\n'
    tool = Tool("Bash", "shell", BASH_SCHEMA)
    schema = response_schema([tool])
    Draft202012Validator.check_schema(schema)
    def fake(prompt, **kwargs):
        response = {"content": "Record no-op", "tool_calls": [
            {"name": "Bash", "arguments": {"command": command, "timeout": None}}]}
        Draft202012Validator(kwargs["output_schema"]).validate(response)
        return CodexResult(text=json.dumps(response), events=[],
                           usage={"input_tokens": 12, "output_tokens": 3}, cli_version="v")
    monkeypatch.setattr("integrations.harnessx.provider.run_codex", fake)
    provider = CodexProvider(CodexSettings("m", tmp_path / "auth"), tmp_path / "calls")
    event = asyncio.run(provider.complete([Message("user", "task")], [tool]))
    assert event.tool_calls[0].input == {"command": command}
    manifest = json.loads(next((tmp_path / "calls").glob("*/transport.json")).read_text())
    assert manifest["argument_encodings"] == {"Bash": "structured_object"}


def test_nested_optional_nulls_omit_only_nonnullable_native_fields():
    from integrations.harnessx.provider import _native_arguments, _strict_arguments, _argument_schema
    from jsonschema import Draft202012Validator
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "options": {"type": "object", "additionalProperties": False, "properties": {
            "limit": {"type": "integer"}}, "required": []},
        "rows": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                 "properties": {"limit": {"type": "integer"}}, "required": []}}}, "required": ["options", "rows"]}
    transported = {"options": {"limit": None}, "rows": [{"limit": None}]}
    Draft202012Validator(_strict_arguments(schema)).validate(transported)
    native = _native_arguments(transported, schema)
    assert native == {"options": {}, "rows": [{}]}
    Draft202012Validator(schema).validate(native)
    # A JSON string preserves both omission and an explicit native null.
    nullable = {"type": "object", "additionalProperties": False, "properties": {
        "value": {"anyOf": [{"type": "string"}, {"type": "null"}]}}, "required": []}
    assert _argument_schema(nullable)["type"] == "string"
    assert _native_arguments({"value": None}, nullable) == {"value": None}


@pytest.mark.parametrize("native_schema,args", [
    ({"type": "object", "additionalProperties": {"type": "string"}}, {"arbitrary": "preserved"}),
    ({"type": "object", "properties": {"value": {"$ref": "#/$defs/Value"}},
      "required": ["value"], "$defs": {"Value": {"type": "string"}}}, {"value": "resolved locally"}),
    ({"type": "object", "properties": {"value": {}}, "required": ["value"]}, {"value": {"anything": [None, 2]}}),
    ({"type": "object", "properties": {"named": {"type": "string"}}}, {"named": "x", "extra": "not lost"}),
    ({"type": "object", "additionalProperties": False, "properties": {
        "nested": {"type": "object", "properties": {"named": {"type": "string"}}}}},
        {"nested": {"named": "x", "extra": "not lost"}}),
    ({"type": "object", "additionalProperties": False, "properties": {
        "value": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}, {"value": None}),
    ({"type": "object", "additionalProperties": False, "properties": {
        "value": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}, {}),
    ({"type": "object", "additionalProperties": False, "properties": {
        "value": {"type": "string"}, "ref": {"$ref": "#/properties/value"}}}, {"ref": "local optional ref"}),
    ({"$schema": "http://json-schema.org/draft-07/schema#", "type": "object", "properties": {
        "pair": {"type": "array", "items": [{"type": "string"}, {"type": "integer"}]}}}, {"pair": ["x", 1]}),
])
def test_custom_schema_lossless_fallback(monkeypatch, tmp_path, native_schema, args):
    fake_events_module(monkeypatch)
    def fake(prompt, **kwargs):
        assert kwargs["output_schema"]["properties"]["tool_calls"]["items"]["anyOf"][0]["properties"]["arguments"]["type"] == "string"
        return CodexResult(text=json.dumps({"content": "", "tool_calls": [
            {"name": "Custom", "arguments": json.dumps(args)}]}), events=[],
            usage={"input_tokens": 12, "output_tokens": 3}, cli_version="v")
    monkeypatch.setattr("integrations.harnessx.provider.run_codex", fake)
    provider = CodexProvider(CodexSettings("m", tmp_path / "auth"), tmp_path / "calls")
    event = asyncio.run(provider.complete([], [Tool("Custom", "custom", native_schema)]))
    assert event.tool_calls[0].input == args


@pytest.mark.parametrize("malformed", [
    {"command": 42, "timeout": None},
    '{"command": "unescaped "quote""}',
    {"command": "echo native"},
])
def test_invalid_transport_retries_once_without_constructing_tools(monkeypatch, tmp_path, malformed):
    fake_events_module(monkeypatch)
    constructions, prompts = [], []
    monkeypatch.setattr(sys.modules["harnessx.core.events"], "ToolCall", lambda **kw: constructions.append(kw))
    def fake(prompt, **kwargs):
        prompts.append(prompt)
        return CodexResult(text=json.dumps({"content": "", "tool_calls": [
            {"name": "Bash", "arguments": malformed}]}), events=[],
            usage={"input_tokens": 12, "output_tokens": 3}, cli_version="v")
    monkeypatch.setattr("integrations.harnessx.provider.run_codex", fake)
    provider = CodexProvider(CodexSettings("m", tmp_path / "auth"), tmp_path / "calls")
    with pytest.raises(ValueError, match="after one serialization retry"):
        asyncio.run(provider.complete([], [Tool("Bash", "shell", BASH_SCHEMA)]))
    assert not constructions
    assert len(prompts) == 2
    assert "rejected before ANY tool execution" in prompts[1]
    assert provider.accounting()["input_tokens"] == 24
    assert provider.accounting()["codex_cli_calls"] == 2
    assert all(c["transport_validation"]["status"] == "invalid" for c in provider.calls)


def test_fallback_malformed_json_retry_keeps_accounting(monkeypatch, tmp_path):
    fake_events_module(monkeypatch)
    count = 0
    def fake(prompt, **kwargs):
        nonlocal count
        count += 1
        args = '{"key":"unescaped "quote""}' if count == 1 else json.dumps({"key": 'a "quote"'})
        return CodexResult(text=json.dumps({"content": "", "tool_calls": [
            {"name": "Custom", "arguments": args}]}), events=[],
            usage={"input_tokens": 12, "output_tokens": 3}, cli_version="v")
    monkeypatch.setattr("integrations.harnessx.provider.run_codex", fake)
    provider = CodexProvider(CodexSettings("m", tmp_path / "auth"), tmp_path / "calls")
    event = asyncio.run(provider.complete([], [Tool("Custom", "map", {
        "type": "object", "additionalProperties": {"type": "string"}})]))
    assert event.tool_calls[0].input == {"key": 'a "quote"'}
    assert event.usage.input_tokens == 24
    assert provider.accounting()["codex_cli_calls"] == 2
    assert [c["transport_validation"]["status"] for c in provider.calls] == ["invalid", "valid"]


def test_provider_distinguishes_external_write_intent_from_local_sandbox(monkeypatch, tmp_path):
    fake_events_module(monkeypatch)
    target = tmp_path / "external-config.yaml"
    def fake(prompt, **kwargs):
        assert "only output is JSON" in prompt
        assert "No local filesystem operation is requested from Codex" in prompt
        assert "Honor all HarnessX scope restrictions and tool denials" in prompt
        assert kwargs["mode"] == "role"
        return CodexResult(text=json.dumps({"content": "Request authorized external write", "tool_calls": [
            {"name": "Write", "arguments": {"file_path": str(target), "content": "processors: []\n"}}]}),
            events=[], usage={"input_tokens": 12, "output_tokens": 3}, cli_version="v")
    monkeypatch.setattr("integrations.harnessx.provider.run_codex", fake)
    schema = {"type": "object", "properties": {"file_path": {"type": "string"},
              "content": {"type": "string"}}, "required": ["file_path", "content"], "additionalProperties": False}
    provider = CodexProvider(CodexSettings("m", tmp_path / "auth"), tmp_path / "calls")
    event = asyncio.run(provider.complete([Message("user", "Write the candidate in the external workspace")],
                                         [Tool("Write", "external workspace write", schema)]))
    assert event.tool_calls[0].input == {"file_path": str(target), "content": "processors: []\n"}
    assert not target.exists()  # Only the external HarnessX executor may perform the action.
