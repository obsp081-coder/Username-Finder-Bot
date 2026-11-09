# Project working rules

- Communicate in concise Modern Standard Arabic. Use English for code, comments, identifiers, and filenames; preserve UTF-8.
- Treat root `PROGRESS.md`, starting with `CURRENT PROJECT STATUS / HANDOFF`, as the single authoritative project handoff. Read it first, then inspect only task-relevant files; do not routinely scan the entire repository or repeat experiments.
- Discuss technical choices and research with the user through ChatGPT first. Write, edit, or test application code only after explicit user approval. Do not automatically produce implementation prompts. Approval for documentation alone does not authorize implementation or application tests.
- Preserve working features and existing data. Prioritize accurate, evidence-supported `Available`, `Taken`, `Reserved`, `Invalid`, and `Unknown` classifications over throughput; missing profiles or HTTP status alone do not prove availability.
- After actually completing a significant approved task, update `CURRENT PROJECT STATUS / HANDOFF` in `PROGRESS.md` with actual modifications, tests performed and results, unresolved issues, and next steps. Do not silently mark unfinished work complete or claim unexecuted tests, live verification, or measurements.
- Preserve detailed historical sections and evidence. Keep the handoff concise instead of duplicating long history; explain superseded facts without erasing the original record. Do not create competing handoff documents.
- ChatGPT-only research and decisions must be explicitly supplied to Codex. Never infer access to other conversations; distinguish user-reported findings from repository-documented evidence and new verification.
- Documentation-only changes require content and Markdown review, not pytest, Ruff, compileall, GUI tests, or live probes. Respect the user's approved scope and keep tool usage efficient.

## GUI Test Execution

- Run compatible Qt regression and UI logic tests with `QT_QPA_PLATFORM=offscreen` in the test child process only. Prefer explicit child-process environment overrides; never change Windows user/machine variables, terminal profiles, `Start.cmd`, or production startup. The existing `tests/conftest.py` default is test-only; an inherited `windows` value must not override the chosen offscreen invocation.
- Until the user explicitly authorizes visible desktop testing, do not open native windows, activate/focus real windows, send desktop keyboard/mouse input, or run visible launcher/smoke helpers. A generic request to test the GUI is not authorization for native desktop testing. In-process Qt test events on offscreen widgets are permitted.
- Defer tests that require a real display, including native Windows rendering, DPI, desktop screenshots and window management. Report them as not run; offscreen layout/event checks do not provide equivalent native coverage. Do not set `UFB_SCREENSHOTS` or opt into native test flags during unattended regression runs.
- Keep testing isolated from production databases and saved operations, use mocked providers, and do not resume jobs or alter provider restrictions. Report actual offscreen results and the deferred native checks separately.

## Git Commit and Push Workflow

- After successfully completing an explicitly approved development or documentation task, automatically commit and push its approved changes. This authorization excludes exploratory research and unapproved modifications.
- Complete the implementation, run appropriate verification, and update `PROGRESS.md` when existing rules require it. Use lightweight content/Markdown checks for documentation-only tasks; keep checks proportional and avoid redundant runs.
- Inspect `git status`, the intended diff, the staged index, the existing remote, intended branch and any outgoing commits. Use the existing GitHub repository `https://github.com/obsp081-coder/Username-Finder-Bot`; do not create another repository or remote or publish unrelated outgoing commits.
- Stage only explicitly named files or hunks belonging to the approved task. Never indiscriminately use `git add -A` or `git add .`, include unrelated changes, or discard/overwrite user work. Preserve unrelated staged changes without including them in the task commit.
- This repository is PUBLIC. Review the actual staged diff and file contents for secrets and private data; do not rely on `.gitignore` alone.
- Never commit or push `runtime/`, user databases, SQLite WAL/SHM files, virtual environments, generated username lists, large exports, passwords, tokens, API credentials, sessions, private user data or temporary diagnostic files, even if already tracked.
- When verification and the staged review pass, create a descriptive commit containing only the approved changes and push it to the verified existing remote and intended branch. Never force-push, automatically rewrite history, or bypass authentication or permissions.
- Verify successful delivery from the push result and the remote branch SHA; report the commit SHA and verified push status. Never claim success without verification.
- If work is incomplete, relevant verification fails, sensitive data is found, or conflicts/authentication/permission errors occur, preserve the work and report the blocker. Do not commit or upload incomplete or unsafe changes, or push changes with known relevant test failures.
