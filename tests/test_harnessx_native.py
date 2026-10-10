"""Full optional HarnessX runtime tests. No Codex calls or Docker are used.

Run with the isolated integration environment and RSI_HARNESSX_CHECKOUT set.
"""
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import shutil
import sys
from types import SimpleNamespace

import pytest

from integrations.harnessx.bundle import freeze_config, load_config
from rsi_eval.codex import CodexResult, CodexSettings


def strict_test_response(response, schema):
    """Scripted model outputs must follow the same strict schema as live Codex."""
    variants = schema["properties"]["tool_calls"]["items"].get("anyOf", [])
    by_name = {row["properties"]["name"]["enum"][0]: row["properties"]["arguments"] for row in variants}
    for call in response["tool_calls"]:
        args_schema = by_name[call["name"]]
        for key in args_schema["required"]:
            call["arguments"].setdefault(key, None)
    from jsonschema import Draft202012Validator
    Draft202012Validator(schema).validate(response)
    return json.dumps(response)


@pytest.fixture
def native_checkout(monkeypatch):
    value = os.environ.get("RSI_HARNESSX_CHECKOUT")
    if not value:
        pytest.skip("Full native runtime requires RSI_HARNESSX_CHECKOUT and integrations/harnessx/requirements.txt")
    root = Path(value).resolve()
    from integrations.harnessx.prepare import check_checkout
    check_checkout(root)
    monkeypatch.syspath_prepend(str(root))
    return root


@pytest.mark.parametrize('cancel_during_cleanup', [False, True])
def test_native_candidate_processor_and_container_tool_execution(native_checkout, monkeypatch, tmp_path, cancel_during_cleanup):
    from harnessx.core.harness import HarnessConfig
    from harbor.models.agent.context import AgentContext
    from integrations.harnessx.harbor_agent import Agent
    source = tmp_path / "source"
    (source / "processors").mkdir(parents=True)
    processor = source / "processors" / "test_hook.py"
    processor.write_text('''from dataclasses import replace
from harnessx import MultiHookProcessor, Message
class Inject(MultiHookProcessor):
    async def on_before_model(self, event):
        yield replace(event, messages=event.messages + (Message(role="user", content="CANDIDATE-HOOK-EXECUTED"),))
''')
    seed = source / "config.yaml"
    seed.write_text(f"tool_registry:\n  builtin: [Bash]\nprocessors:\n- _target_: file://{processor}::Inject\n")
    bundle = tmp_path / "candidate"
    freeze_config(seed, bundle)
    snapshot = tmp_path / "snapshot"
    shutil.copytree(bundle, snapshot)
    shutil.rmtree(bundle)
    shutil.rmtree(source)
    calls, commands = [], []

    def fake(prompt, **kwargs):
        assert "CANDIDATE-HOOK-EXECUTED" in prompt
        calls.append(prompt)
        response = {"content": "done", "tool_calls": []}
        if len(calls) == 1:
            response = {"content": "", "tool_calls": [{"name": "Bash", "arguments": {"command": "echo candidate"}}]}
        return CodexResult(text=strict_test_response(response, kwargs["output_schema"]), events=[],
            usage={"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 1}, cli_version="test")
    monkeypatch.setattr("integrations.harnessx.provider.run_codex", fake)
    if cancel_during_cleanup:
        async def cancelled_cleanup(self):
            raise asyncio.CancelledError()
        monkeypatch.setattr("integrations.harnessx.harbor_agent.TaskMCPBridge.close", cancelled_cleanup)

    class Environment:
        async def exec(self, command, **kwargs):
            commands.append(command)
            return SimpleNamespace(stdout="/app\n" if command == "pwd" else "candidate\n", stderr="", return_code=0)

    async def execute():
        agent = Agent(logs_dir=tmp_path / "logs", artifact_dir=snapshot,
            harnessx_checkout=native_checkout, codex_auth_dir=tmp_path / "auth-unused",
            codex_version="test", model_name="test")
        environment, context = Environment(), AgentContext()
        await agent.setup(environment)
        if cancel_during_cleanup:
            with pytest.raises(asyncio.CancelledError):
                await agent.run("task", environment, context)
        else:
            await agent.run("task", environment, context)
        return context

    context = asyncio.run(execute())
    assert "echo candidate" in commands
    assert len(calls) == 2
    assert context.n_input_tokens == 20
    assert context.n_output_tokens == 4
    assert context.cost_usd is None
    assert (tmp_path / "logs" / "trajectory.md").is_file()


def test_native_metaagent_two_rounds_retains_gates_and_history(native_checkout, monkeypatch, tmp_path):
    from harnessx import ModelConfig
    from harnessx.meta_harness.agent import MetaAgent
    from harnessx.meta_harness.journal import JournalEntrySpec, append_entry, fill_gating
    from benchmarks.terminal_bench_2.harness import make_tb2_harness_config
    from integrations.harnessx.provider import CodexProvider

    monkeypatch.setenv("HARNESSX_HOME", str(tmp_path / "home"))
    seed = tmp_path / "seed.yaml"
    make_tb2_harness_config().to_yaml_file(seed)
    feedback = tmp_path / "feedback"
    feedback.mkdir()
    (feedback / "trial.md").write_text("Task failed after repeated edits; reward=0; inspect the edit threshold.")
    memo = tmp_path / "learnings.md"
    current_round = {"index": 0, "phase": 0, "output": None, "yaml": seed.read_text()}
    prompts = []

    def fake(prompt, **kwargs):
        prompts.append(prompt)
        response = {"content": "OK", "tool_calls": []}
        if "Synthetic replay smoke check. Reply with exactly: OK" not in prompt:
            phase = current_round["phase"]
            out = current_round["output"]
            index = current_round["index"]
            if phase == 0:
                response = {"content": "Read the evidence", "tool_calls": [{"name": "Read", "arguments": {
                    "file_path": str(out / "_meta_scratch" / "TASK.md")}}]}
            elif phase == 1:
                read_path = feedback / "trial.md" if index == 1 else out / "_meta_scratch" / "CONTEXT.md"
                response = {"content": "Read feedback and history", "tool_calls": [{"name": "Read", "arguments": {"file_path": str(read_path)}}]}
            elif phase == 2:
                text = re.sub(r"threshold: \d+", f"threshold: {7 + index}", current_round["yaml"], count=1)
                response = {"content": "Adjust repeated-edit warning", "tool_calls": [{"name": "Write", "arguments": {
                    "file_path": str(out / "config.yaml"), "content": text}}]}
            elif phase == 3:
                response = {"content": "Record evidence", "tool_calls": [{"name": "Write", "arguments": {
                    "file_path": str(out / "_meta_scratch" / "candidates.md"),
                    "content": f"## Candidate C-{index:03d}\n[lens: failure | lever: control | intent: corrective]\nRepeated edits in trial.md motivate a threshold change."}}]}
            elif phase == 4:
                scratch_memo = tmp_path / "fixture-journal.md"
                if memo.exists():
                    scratch_memo.write_text(memo.read_text())
                append_entry(scratch_memo, JournalEntrySpec(round=index, label=f"Edit threshold {index}",
                    hypothesis_id=f"edit-threshold-{index}", levers=["control"], predicted_affected=["task"],
                    prose="Read previous feedback and change the warning threshold.",
                    extra_frontmatter={"cited_candidates": [f"C-{index:03d}"]}))
                response = {"content": "Journal the hypothesis", "tool_calls": [{"name": "Write", "arguments": {
                    "file_path": str(memo), "content": scratch_memo.read_text()}}]}
            current_round["phase"] += 1
        return CodexResult(text=strict_test_response(response, kwargs["output_schema"]), events=[],
            usage={"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 0}, cli_version="test")

    monkeypatch.setattr("integrations.harnessx.provider.run_codex", fake)
    provider = CodexProvider(CodexSettings("test", tmp_path / "auth-unused"), tmp_path / "calls")
    model = ModelConfig(main=provider)
    meta = MetaAgent(inner_model=model, memo_path=memo, max_cost_usd=None, max_steps=12, wall_clock_s=30)

    async def execute():
        current = seed
        for index in (1, 2):
            output = tmp_path / f"R{index}"
            current_round.update(index=index, phase=0, output=output, yaml=current.read_text())
            current = await meta.evolve(current_config=current, trajectories_dir=feedback, output_dir=output,
                replay_model=model, replay_max_cost_usd=None, replay_timeout_s=20)
            fill_gating(memo, index, "accepted", {"task": "still_F"})
            assert json.loads((output / "_meta_scratch" / "changeset.json").read_text())
            assert (output / "_meta_scratch" / "REPLAY.md").is_file()
        return current

    result = asyncio.run(execute())
    assert result.is_file()
    assert "Journal Context for R2" in "\n".join(prompts)
    assert provider.accounting()["codex_cli_calls"] >= 12
    from integrations.harnessx.launcher import history_reads
    assert history_reads(tmp_path / "R2", memo)


def test_all_native_meta_and_target_tools_have_valid_transport(native_checkout, monkeypatch, tmp_path):
    from jsonschema import Draft202012Validator
    from harnessx import ModelConfig
    from harnessx.meta_harness.agent import build_meta_agent_harness_config
    from benchmarks.terminal_bench_2.harness import make_tb2_harness_config
    from integrations.harnessx.provider import CodexProvider, response_schema, _native_named_schema

    provider = CodexProvider(CodexSettings("test", tmp_path / "auth"), tmp_path / "calls")
    meta = build_meta_agent_harness_config(inner_model=ModelConfig(main=provider),
                                         workspace_root=tmp_path / "workspace", max_cost_usd=None)
    tool_sets = [meta.tool_registry.get_schemas(), make_tb2_harness_config().tool_registry.get_schemas(), []]
    assert {t.name for t in tool_sets[0]} == {
        "Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebSearch", "WebFetch", "spawn_reflect_worker"}

    def sample(schema):
        if "anyOf" in schema:
            if any(part.get("type") == "null" for part in schema["anyOf"]):
                return None
            return sample(schema["anyOf"][0])
        if "enum" in schema:
            return schema["enum"][0]
        kind = schema["type"]
        if kind == "object":
            return {name: sample(part) for name, part in schema["properties"].items()}
        if kind == "array":
            return []
        return {"string": "test", "integer": 1, "number": 1, "boolean": False, "null": None}[kind]

    def fake(prompt, **kwargs):
        schema = kwargs["output_schema"]
        variants = schema["properties"]["tool_calls"]["items"].get("anyOf", [])
        response = {"content": "Native tools own execution", "tool_calls": [sample(row) for row in variants]}
        Draft202012Validator(schema).validate(response)
        return CodexResult(text=json.dumps(response), events=[],
                           usage={"input_tokens": 10, "output_tokens": 2}, cli_version="test")

    monkeypatch.setattr("integrations.harnessx.provider.run_codex", fake)
    for tools in tool_sets:
        assert all(_native_named_schema(tool) for tool in tools)
        Draft202012Validator.check_schema(response_schema(tools))
        event = asyncio.run(provider.complete([], tools))
        assert len(event.tool_calls) == len(tools)
        for call, tool in zip(event.tool_calls, tools):
            Draft202012Validator(tool.input_schema).validate(call.input)
