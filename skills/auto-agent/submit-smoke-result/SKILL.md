---
name: submit-smoke-result
description: Persist the smoke-agent's runtime verdict — proof that the code in the workspace actually runs end-to-end. Use after you've installed dependencies, exercised routes, screenshotted UI, and/or run the project's test suite for the change under review.
---

<what-to-do>

Write your smoke-test verdict to `.auto-agent/smoke_result.json` in the workspace, then stop.

The file must be valid JSON with this exact shape:

```json
{
  "schema_version": "1",
  "verdict": "pass",
  "summary": "<one paragraph summarising what you actually did and what passed>",
  "attempts": [
    {
      "step": "<short label, e.g. 'install', 'boot', 'route /api/foo', 'pytest'>",
      "command": "<the shell command you ran>",
      "exit_code": 0,
      "ok": true,
      "output_preview": "<last ~500 chars of stdout/stderr — useful for the human reviewer>"
    }
  ],
  "failures": [],
  "proposed_smoke_yml": ""
}
```

`verdict` is one of:
- `"pass"` — at least one *real* runtime check ran and succeeded (boot, route, or test suite). The diff demonstrably runs.
- `"fail"` — a runtime check ran and failed (boot didn't come up, a route returned 5xx or a stub-shaped body, tests failed, types failed, install crashed).
- `"skipped"` — only allowed when the diff is markdown / docs / comments only AND there is no test suite to run. Anything else MUST be `"fail"` instead.

`failures` is a list of one-line summaries when `verdict="fail"` (empty otherwise).

`proposed_smoke_yml` is optional — set it to a YAML string the user can drop into the repo root as `auto-agent.smoke.yml` when you had to infer the boot command. Leave empty if you didn't need to infer.

Use Write (not Edit) to create the file — create the `.auto-agent/` directory first if it doesn't exist.

</what-to-do>

<rules>

- `schema_version` must be the string `"1"` literally.
- The path is exactly `.auto-agent/smoke_result.json` at the workspace root.
- `verdict="pass"` requires that you ran at least one of: `boot_dev_server` + a route hit, the project's test suite, a build/typecheck command. "I read the code and it looks correct" is NEVER `pass`.
- `verdict="skipped"` is a last resort and must be justified in `summary`. If the diff touches any `.py`, `.ts`, `.tsx`, `.js`, `.go`, `.rs`, or similar code file, you may not skip — run *something* (at minimum, the test suite).
- Do not output the JSON in the chat — only write the file.
- The orchestrator reads `.auto-agent/smoke_result.json` after your turn returns; that is the only signal it needs from you here.

</rules>
