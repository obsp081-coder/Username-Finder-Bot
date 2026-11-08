# Project working rules

- Communicate in concise Modern Standard Arabic. Use English for code, comments, identifiers, and filenames; preserve UTF-8.
- Treat root `PROGRESS.md`, starting with `CURRENT PROJECT STATUS / HANDOFF`, as the single authoritative project handoff. Read it first, then inspect only task-relevant files; do not routinely scan the entire repository or repeat experiments.
- Discuss technical choices and research with the user through ChatGPT first. Write, edit, or test application code only after explicit user approval. Do not automatically produce implementation prompts. Approval for documentation alone does not authorize implementation or application tests.
- Preserve working features and existing data. Prioritize accurate, evidence-supported `Available`, `Taken`, `Reserved`, `Invalid`, and `Unknown` classifications over throughput; missing profiles or HTTP status alone do not prove availability.
- After actually completing a significant approved task, update `CURRENT PROJECT STATUS / HANDOFF` in `PROGRESS.md` with actual modifications, tests performed and results, unresolved issues, and next steps. Do not silently mark unfinished work complete or claim unexecuted tests, live verification, or measurements.
- Preserve detailed historical sections and evidence. Keep the handoff concise instead of duplicating long history; explain superseded facts without erasing the original record. Do not create competing handoff documents.
- ChatGPT-only research and decisions must be explicitly supplied to Codex. Never infer access to other conversations; distinguish user-reported findings from repository-documented evidence and new verification.
- Documentation-only changes require content and Markdown review, not pytest, Ruff, compileall, GUI tests, or live probes. Respect the user's approved scope and keep tool usage efficient.
