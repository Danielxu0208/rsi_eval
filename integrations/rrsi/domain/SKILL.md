# Codex coding harness evolution

The candidate is a small Python harness around a frozen Codex policy. The
runner loads this candidate's `agent.py`, `prompt.md` and `tools/` in each
benchmark trial. Read the current seed before editing: preserve its public
entry points and return types. The trusted runner, model, authentication,
timeouts, task selection and verifier are outside the editable directory.

Use the existing RRSI JSON actions to inspect and edit files. Every declared
edit must explain a general mechanism and match a real code or prompt change.
Reason from the supplied trajectories and history. Improve prompts, bounded
control flow, context handling or reusable tools only through interfaces that
the current wrapper actually executes. A file that is never loaded is not an
improvement. Keep baseline behavior working when no optional tool is present.

Never embed benchmark names, task IDs, answers, task-specific paths or copied
task observations. Do not read verifier files or persist task-specific state
across trials. Retain bounded termination, error handling and output limits.
Do not modify model names, authentication, external evaluators or budgets.
The critic and native RRSI selection rule decide whether changes are accepted;
do not try to predict or directly modify evaluation scores.

