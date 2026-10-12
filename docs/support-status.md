# Compatibility and runtime requirements

Current benchmark configurations, execution prerequisites, method input capabilities and objective requirements.

Query the packaged configuration from the CLI or Python. These commands make no model calls, start no containers and inspect no credentials.

```sh
rsi-eval status --benchmark terminal-bench@2.0 --method rrsi
rsi-eval status --benchmark gpqa --json
```

```python
from rsi_eval import support_status

record = support_status(benchmark="drop@1.0-agent", method="ahe")
```

An exact `name@version` selects one configuration. A bare benchmark name returns every catalog version, including labeled legacy subsets. Unknown names and versions fail explicitly. Process-local custom registrations expose their caller-declared configuration. Use `check` to inspect the configured runtime before execution.

## Shared runtime requirements

- Python 3.12 or newer and Harbor 0.18.0 in its execution environment.
- Working Docker/Compose with the task image architecture; macOS uses a Linux VM.
- Target model access and task-specific assets; subscription access does not supply native judge credentials.

## Configuration overview

Task counts describe selectable entries in each declared configuration.

| Exact configuration | Tasks | Catalog scope | Reward domain |
| --- | ---: | --- | --- |
| `terminal-bench@2.0` | 89 | complete-pinned-Harbor-release | binary |
| `terminal-bench@2.1` | 89 | complete-pinned-Harbor-release | binary |
| `swe-bench-verified@1` | 500 | complete-pinned-Harbor-release | binary |
| `apex-agents@1.1.1` | 240 | complete-public-pinned-release | unit_interval |
| `drop@1.0-agent` | 86945 | complete-public-pinned-release | unit_interval |
| `engdesign-open@open-subset-c8ec338-v1` | 2 | reviewed-open-subset | binary |
| `engdesign-open@open-c8ec338-v1` | 67 | full-upstream-open | binary |
| `frontier-eng@cpu-subset-bafa7d5-v1` | 2 | reviewed-cpu-subset | heterogeneous |
| `frontier-eng@v1-bafa7d5` | 48 | full-v1-with-additional-engdesign-entry | heterogeneous |
| `aider-polyglot@1.0-harbor` | 225 | complete-pinned-Harbor-release | binary |
| `swe-bench-multilingual@1.0-harbor` | 300 | complete-pinned-Harbor-release | binary |
| `swe-bench-pro@1.0-harbor` | 731 | complete-pinned-Harbor-release | binary |
| `harvey-lab@public-a15650f-v1` | 2010 | full-public-pinned-version | unit_interval |
| `jobbench@d43a665-660b33a-v1` | 128 | complete-public-pinned-splits | unit_interval |
| `livecodebench@release_v1` | 400 | complete-public-pinned-release | binary |
| `mce-finer@meta-harness-8123cca-v1` | 400 | all-published-mce-configuration-splits | binary |
| `mce-uspto@meta-harness-8123cca-v1` | 180 | all-published-mce-configuration-splits | binary |
| `mce-symptom2disease@meta-harness-8123cca-v1` | 462 | all-published-mce-configuration-splits | binary |
| `mce-lawbench@meta-harness-8123cca-v1` | 350 | all-published-mce-configuration-splits | binary |
| `mce-aegis@meta-harness-8123cca-v1` | 668 | all-published-mce-configuration-splits | binary |
| `mgsm@1.0-agent` | 2750 | complete-public-pinned-release | binary |
| `mmlu@original-all-splits-v1` | 115700 | all-public-original-splits | binary |
| `gpqa@main-56686c0-v1` | 448 | complete-public-configuration | binary |
| `gpqa@diamond-56686c0-v1` | 198 | complete-public-configuration | binary |
| `gpqa@extended-56686c0-v1` | 546 | complete-public-configuration | binary |
| `arc-agi@1-public-3990304-v1` | 800 | all-public-training-and-evaluation | binary |

## Method objectives and input capabilities

### meta-harness

Codex adaptation of upstream `8123ccabe2b19fa1123090b2e5f1bacc20963ce8`. Codex wrapper through Harbor, including task-provided MCP.

| Input modality | Transport support |
| --- | --- |
| text | Supported by the Codex adaptation |
| image | Determined by the selected target and task tools |
| audio | Determined by the selected target and task tools |
| video | Determined by the selected target and task tools |

Execution limits:

- The selected target and task tools determine input handling.
- Native method gates, task budgets and runtime prerequisites remain in force.

| Benchmark reward domain | Requirement |
| --- | --- |
| binary | native objective |
| unit_interval | explicit binary score mapping required |
| heterogeneous | explicit binary score mapping required |

Native validity and selection gates remain mandatory; explicit selection callbacks may choose only eligible measured candidates.

### rrsi

Codex adaptation of upstream `be50316e1db05914068a973f322770ef08ed7ba1`. Codex wrapper through Harbor, including task-provided MCP.

| Input modality | Transport support |
| --- | --- |
| text | Supported by the Codex adaptation |
| image | Determined by the selected target and task tools |
| audio | Determined by the selected target and task tools |
| video | Determined by the selected target and task tools |

Execution limits:

- The selected target and task tools determine input handling.
- Native method gates, task budgets and runtime prerequisites remain in force.

| Benchmark reward domain | Requirement |
| --- | --- |
| binary | native objective |
| unit_interval | native score range; caller chooses a meaningful objective |
| heterogeneous | explicit bounded score mapping required |

Native validity and selection gates remain mandatory; explicit selection callbacks may choose only eligible measured candidates.

### ahe

Codex adaptation of upstream `8b2a55d97590363fe50c3cc6b5e833b020a4bb4c`. Codex wrapper through Harbor, including task-provided MCP.

| Input modality | Transport support |
| --- | --- |
| text | Supported by the Codex adaptation |
| image | Determined by the selected target and task tools |
| audio | Determined by the selected target and task tools |
| video | Determined by the selected target and task tools |

Execution limits:

- The selected target and task tools determine input handling.
- Native method gates, task budgets and runtime prerequisites remain in force.

| Benchmark reward domain | Requirement |
| --- | --- |
| binary | native objective |
| unit_interval | explicit binary score mapping required |
| heterogeneous | explicit binary score mapping required |

Native validity and selection gates remain mandatory; explicit selection callbacks may choose only eligible measured candidates.

### harnessx

Codex adaptation of upstream `915520584c92cc35ed24aef1af17eec1d60c0b01`. HarnessX native processors/tool loop with Harbor MCP bridge.

| Input modality | Transport support |
| --- | --- |
| text | Supported by the Codex role transport |
| image | Static PNG, JPEG and WebP through native Codex image attachments |
| audio | Unsupported; no transcription fallback |
| video | Unsupported |

Execution limits:

- GIF and animated PNG/WebP are unsupported; rejected media that reaches the provider stops before another model call.
- Transport limits: 20 MiB per image and 64 MiB per turn. The native tool-result budget is 200,000 characters including base64; that stage can remove oversized images with a notice before the provider receives them.
- Remote URLs and local-path media blocks are rejected. Native task MCP images and embedded image resources use the same attachment route.
- Native method gates, task budgets and runtime prerequisites remain in force.

| Benchmark reward domain | Requirement |
| --- | --- |
| binary | native objective |
| unit_interval | native score range; caller chooses a meaningful objective |
| heterogeneous | finite score; declare objective and selection for unlike task metrics |

Native validity and selection gates remain mandatory; explicit selection callbacks may choose only eligible measured candidates.

## Benchmark protocols and prerequisites

### terminal-bench@2.0

Dataset identity: `sha256:c6fc2e2382c1dbae99b2d5ecd2f4f4a60c3c01e0d84642d69b4afd92e99d078b`.

Protocol: Original pinned Harbor task instructions, environment and verifier.

Reward domain: `binary`. Lifecycle: `active`.

### terminal-bench@2.1

Dataset identity: `sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a`.

Protocol: Original pinned Harbor task instructions, environment and verifier.

Reward domain: `binary`. Lifecycle: `active`.

### swe-bench-verified@1

Dataset identity: `sha256:b934b0cc3dc800fe945eaf9f1623329db97ee3133c706d20644524c7759fb341`.

Protocol: Original pinned Harbor task instructions, environment and verifier.

Reward domain: `binary`. Lifecycle: `active`.

### apex-agents@1.1.1

Dataset identity: `mercor/apex-agents-1-1@1.1.1`.

Protocol: Native APEX-Agents 1.1.1 Harbor tasks: immutable agent/world/verifier images, original MCP environment, rubric grader and snapshot collection. The selected target runs in the native agent environment.

Reward domain: `unit_interval`. Lifecycle: `active`.

Additional requirements: Native Vercel AI Gateway judge credentials, plus original world services.

- The selected target must connect the native task world MCP service.

### drop@1.0-agent

Dataset identity: `allenai/drop@sha256:39d2278a29fd729de301b111a45f434c24834f40df8f4ff116d864589e3249d6`.

Protocol: zero-shot-agent-drop-json-answer.

Reward domain: `unit_interval`. Lifecycle: `active`.

- Task-assisted agent protocol with original scoring.
- All 86,945 released train/dev records remain selectable. Nine empty-reference train records retain native zero scoring and have no gold-transport oracle.

### engdesign-open@open-subset-c8ec338-v1

Dataset identity: `git:c8ec338cc5f846d792d93728b00c9448111139e5`.

Protocol: engdesign-native-verifier.

Reward domain: `binary`. Lifecycle: `legacy-subset`.

Additional requirements: Task-specific scientific dependencies, Octave/Verilog and configured Webots assets where required.

- Legacy subset retained for reproducibility; select the newer full configuration for all released tasks.
- Native graders retain their original behavior and same-interpreter execution.

### engdesign-open@open-c8ec338-v1

Dataset identity: `git:c8ec338cc5f846d792d93728b00c9448111139e5`.

Protocol: engdesign-native-verifier.

Reward domain: `binary`. Lifecycle: `active`.

Additional requirements: Task-specific scientific dependencies, Octave/Verilog and configured Webots assets where required.

- Native graders retain their original behavior and same-interpreter execution.

### frontier-eng@cpu-subset-bafa7d5-v1

Dataset identity: `git:bafa7d53162a3b1f128fec396bbde22fe972bf24`.

Protocol: frontier-native-feedback.

Reward domain: `heterogeneous`. Lifecycle: `legacy-subset`.

Additional requirements: Task-specific downloaded assets and grader namespace capabilities.

- Legacy subset retained for reproducibility; select the newer full configuration for all released tasks.
- Native continuous objectives stay per-task; their metric scales and validity rules differ.

### frontier-eng@v1-bafa7d5

Dataset identity: `git:bafa7d53162a3b1f128fec396bbde22fe972bf24`.

Protocol: frontier-native-unified-feedback.

Reward domain: `heterogeneous`. Lifecycle: `active`.

Additional requirements: Task-specific downloaded assets and grader namespace capabilities; NVIDIA/CUDA runtime for GPU task selections.

- Native continuous objectives stay per-task; their metric scales and validity rules differ.

### aider-polyglot@1.0-harbor

Dataset identity: `sha256:01e28d85e46beae5b7e29a29f57cb49d882b5486583d52cec4ee5bf3540a1c84`.

Protocol: harbor-port.

Reward domain: `binary`. Lifecycle: `active`.

- Harbor port retains the task tests and uses Harbor's agent workflow; Aider's native two-edit retry controller and edit-format metrics are separate.

### swe-bench-multilingual@1.0-harbor

Dataset identity: `manifest-sha256:3f6a80369441bf976d4467c6f371d1e75836795ed5d15a41f4a4473eb0e9ed23`.

Protocol: harbor-port.

Reward domain: `binary`. Lifecycle: `active`.

- Pinned Harbor port includes available hints; this is an agent variant with native test grading.

### swe-bench-pro@1.0-harbor

Dataset identity: `sha256:88411d32ff27e53a4c1a7e29f0c2aeba180c8e5d60f221cab5ed56325f33549d`.

Protocol: harbor-port.

Reward domain: `binary`. Lifecycle: `active`.

Additional requirements: linux/amd64 task images.

- Harbor port includes declared execution changes, including Jest worker/exit options.

### harvey-lab@public-a15650f-v1

Dataset identity: `harveyai/harvey-labs@a15650fe74bb20b86f160aaabdad75b87a91b6e8`.

Protocol: harbor-native-lab-dual-judge-v1.

Reward domain: `unit_interval`. Lifecycle: `active`.

Additional requirements: Native Anthropic and OpenAI judge credentials; default dual-judge protocol.

- No published gold deliverables; a gold-deliverable oracle is unavailable.

### jobbench@d43a665-660b33a-v1

Dataset identity: `JobBench/job-bench@d43a66575635da0d3f6cb9a20976dc1a60f3ffc9`.

Protocol: jobbench-native-harbor.

Reward domain: `unit_interval`. Lifecycle: `active`.

Additional requirements: Native xAI judge credentials; grok-4.3.

- No published gold deliverables; a gold-deliverable oracle is unavailable.

### livecodebench@release_v1

Dataset identity: `hf:livecodebench/code_generation_lite@0fe84c3912ea0c4d4a78037083943e8f0c4dd505:release_v1`.

Protocol: Original pinned Harbor task instructions, environment and verifier.

Reward domain: `binary`. Lifecycle: `active`.

- Complete 400-task candidate set; reference solutions cover only a subset. Repetitions are independent trials.

### mce-finer@meta-harness-8123cca-v1

Dataset identity: `mce-finer@meta-harness-8123cca-v1`.

Protocol: meta-harness-mce-native-per-record-v1.

Reward domain: `binary`. Lifecycle: `active`.

- Task-assisted agent protocol with original scoring.

### mce-uspto@meta-harness-8123cca-v1

Dataset identity: `mce-uspto@meta-harness-8123cca-v1`.

Protocol: meta-harness-mce-native-per-record-v1.

Reward domain: `binary`. Lifecycle: `active`.

- Task-assisted agent protocol with original scoring.

### mce-symptom2disease@meta-harness-8123cca-v1

Dataset identity: `mce-symptom2disease@meta-harness-8123cca-v1`.

Protocol: meta-harness-mce-native-per-record-v1.

Reward domain: `binary`. Lifecycle: `active`.

- Task-assisted agent protocol with original scoring.

### mce-lawbench@meta-harness-8123cca-v1

Dataset identity: `mce-lawbench@meta-harness-8123cca-v1`.

Protocol: meta-harness-mce-native-per-record-v1.

Reward domain: `binary`. Lifecycle: `active`.

- Task-assisted agent protocol with original scoring.

### mce-aegis@meta-harness-8123cca-v1

Dataset identity: `mce-aegis@meta-harness-8123cca-v1`.

Protocol: meta-harness-mce-native-per-record-v1.

Reward domain: `binary`. Lifecycle: `active`.

- Task-assisted agent protocol with original scoring.

### mgsm@1.0-agent

Dataset identity: `google-research/url-nlp@452a21ad3dae5668c06ceeac21ff073e1e40f9be/mgsm`.

Protocol: zero-shot-agent-numeric-answer.

Reward domain: `binary`. Lifecycle: `active`.

- Task-assisted agent protocol with original scoring.

### mmlu@original-all-splits-v1

Dataset identity: `mmlu@original-all-splits-v1`.

Protocol: mmlu-agent-choice-5shot-heldout-v1.

Reward domain: `binary`. Lifecycle: `active`.

- Task-assisted agent protocol with original scoring.

### gpqa@main-56686c0-v1

Dataset identity: `gpqa@main-56686c0-v1`.

Protocol: gpqa-agent-choice-seed0-v1.

Reward domain: `binary`. Lifecycle: `active`.

- Task-assisted agent protocol with original scoring.

### gpqa@diamond-56686c0-v1

Dataset identity: `gpqa@diamond-56686c0-v1`.

Protocol: gpqa-agent-choice-seed0-v1.

Reward domain: `binary`. Lifecycle: `active`.

- Task-assisted agent protocol with original scoring.

### gpqa@extended-56686c0-v1

Dataset identity: `gpqa@extended-56686c0-v1`.

Protocol: gpqa-agent-choice-seed0-v1.

Reward domain: `binary`. Lifecycle: `active`.

- Task-assisted agent protocol with original scoring.

### arc-agi@1-public-3990304-v1

Dataset identity: `arc-agi@1-public-3990304-v1`.

Protocol: arc-agi-1-exact-three-attempts-v1.

Reward domain: `binary`. Lifecycle: `active`.

- Task-assisted agent protocol with original scoring.
