# Workspace and reasoning expansion

> Historical expansion record. GDPval was subsequently removed from the active
> package. The current catalog contains 26 configurations: 24 evaluation profiles
> and two legacy subsets. Results and counts below describe the original audit.
> See the [README](../README.md) for the current support list.

This expansion adds 14 pinned configurations: 13 evaluation adapters and one
GDPval data/export configuration whose native automatic grading is blocked.
Together with the previous releases, `list` exposes 27 configurations: 24
evaluation profiles, the blocked GDPval profile and two labeled legacy subsets.
Omitting `task_ids` selects every task in the declared version. It does not
claim access to unpublished tasks or merge different releases into one score.

| Family | Complete declared coverage | Validation and remaining requirement |
| --- | --- | --- |
| MMLU | 115,700 records in all four released splits; test is 14,042 | All-record source/scorer audit; real Docker oracle |
| GPQA | main 448, diamond 198, extended 546 | All three full releases audited; one Docker oracle each |
| ARC-AGI-1 | 400 public training + 400 public evaluation tasks | All-record three-attempt scoring audit; Docker oracle |
| MCE | FiNER 400, USPTO 180, Symptom2Disease 462, LawBench crime prediction 350, AEGIS 668 | Complete published MCE configurations, native prompts/scorers; five Docker oracles |
| JobBench | 65 main + 63 easy tasks | All 878 source files verified; 128 native empty submissions; original verifier and Office extraction exercised; nonempty grading needs xAI API access |
| Harvey LAB | 2,010 tasks, 27 practice areas | 60,971 source documents verified, all tasks materialized and native schemas checked; native container grader tested with a declared deterministic fixture; real dual judging needs Anthropic and OpenAI API access |
| APEX-Agents 1.1.1 | 240 tasks across 31 worlds | All original packages and 33 image manifests verified; one actual world, 74 tools, file changes and native snapshots checked; native judging needs Vercel AI Gateway access |
| GDPval public gold | 220 public tasks, all 509 reference/deliverable assets mapped | Complete metadata export and source manifest audit; one task's files downloaded; native automatic grading unavailable after the hosted service retired |

The MCE profiles cover those published experimental datasets, not every task in
the underlying LawBench or other source corpus. GPQA releases overlap; their
counts are not counts of unique questions. APEX 1.1.1 is distinct from the
original 480-task 1.0 release. GDPval's other 1,100 paper tasks are not public.
No subset is presented as the full private benchmark.

## Evidence and scope

- [Reasoning/MCE all-record audit](validation-reasoning-mce-native.json):
  119,752 configuration records, including overlapping GPQA releases. Every
  source identity was checked; gold and missing-answer cases exercised the
  native score rules.
- [Reasoning/MCE Docker checks](validation-reasoning-mce-oracle.json): ten real
  Harbor oracle trials, one per new configuration, each with reward 1. These
  calls use source answers to check transport and scoring, not model ability.
- [JobBench evidence](validation-jobbench.json) and [setup](jobbench.md).
  Empty submissions exercise the native zero rule without invoking a judge.
  They do not validate the quality or calibration of paid model judgments.
- [Harvey evidence](validation-harvey-lab.json) and [setup](harvey-lab.md).
  Deterministic judge responses are labeled test fixtures, not real model grades.
- [APEX protocol review](validation-apex-review.json) and [setup](apex-agents.md).
  The adapter preserves original graders, world configuration and all 34 file
  tasks' collect hooks. A recorded cache-only repair fixes service-user startup
  permissions without changing tool commands, users or data access. No snapshot
  failure becomes a task score.
- [GDPval evidence](validation-gdpval.json) and [limitation](gdpval.md).
  Its complete public export is useful for external human review, but is not a
  successful `evaluate()` run.

The Codex MCP adapter was also exercised with a real subscription and an
explicit local test server. An initial call discovered the tool but could not
approve it noninteractively. The corrected task-server configuration completed
one tool call and returned its receipt. Both attempts' usage is retained; no API
price was assigned to subscription tokens. This is transport evidence, not an
APEX score or a new method experiment.

The latest native container and installation results are recorded in
[runtime evidence](validation-workspace-runtime.json) and
[packaging evidence](validation-workspace-packaging.json). Old acceptance and
coverage reports retain their original scopes and counts. This expansion does
not rerun full model benchmarks or reproduce any paper's scores.

## Use from a method

The SDK remains `evaluate(target=..., benchmark=..., options=..., output_dir=...)`.
Each benchmark has an example under `examples/benchmarks/`; preview it offline,
then run `check` before evaluation. An omitted task list selects the full
configuration. The user chooses task sets; source splits and publisher terms
remain visible.

Judge credentials can come from a private `judge_env_file`. Only its path is
stored in configuration; credential values are injected into the separate
verifier process. Missing credentials stop before a target model call. Judge
authentication, quota and infrastructure errors remain distinct from measured
task failure. Judge usage is unknown when the original grader does not provide
it; it is never mixed into the evaluated agent's tokens.

The two bundled methods now forward runtime settings through subprocesses and
candidate worktrees. Their native selection rules are retained: Meta-Harness
accepts binary evolve scores and rejects continuous rubric profiles; RRSI can
consume comparable rewards in `[0, 1]`. A custom method can use any raw metric.
Neither method has been experimentally rerun on every new benchmark.

## Remaining external blockers

The present account has a Codex subscription but no native judge keys for
JobBench, Harvey or APEX. Their complete task adapters and grading paths are
implemented, but paid end-to-end grading cannot be certified in this environment.
Replacing those judges with Codex would change the benchmark protocol and has
not been done.

GDPval has a different limitation: the official hosted grading service and
leaderboard retired. The published reference is human expert preference;
an approximate model judge would be a separately declared protocol. The profile
stays visible as `grading-blocked` so the project owner can choose whether to
keep the human-review/export route or request a distinct automatic protocol.
