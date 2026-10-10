# General mechanisms worth testing

- Make task requirements and completion conditions explicit in the policy prompt.
- Preserve useful error observations when shortening repeated output.
- Use bounded retries with a stated failure exit for recoverable tool errors.
- Prefer reusable procedures over task-specific instructions or stored answers.
- Add or alter a tool only when its interface is used by the current seed.
- Retain changes only through RRSI's existing critic and empirical selection.

These are hypotheses, not mandatory edits. Inspect the current implementation
and recorded traces before selecting one. Avoid repeating hypotheses already
rejected in the edit history.
