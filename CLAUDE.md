# agenthub — notes for coding agents

- Python 3.11+, standard library only. No third-party imports in `agenthub/`.
- Design is the source of truth: `docs/design.md`, `docs/protocol.md`. A behaviour change updates them in the same change.
- Unit tests: `python3 -m unittest discover -s tests`. Integration tests touch the real systemd user manager and run only with `AGENTHUB_IT=1`; they use the unit prefix `agenthubtest` and remove what they create.
- Keep test load small: memory estimates instead of real allocations, CPU burners pinned to one core for a few seconds.
- This repository is public: no personal paths, names, hostnames or e-mail addresses in code, docs, tests or commit messages.
- Task notes live in `.claude/` (ignored by git).
- Git: agents commit and push to `main` on their own, after the tests pass (owner's decision, 2026-11-16).
  Commit messages in English; commits use the repo-local GitHub noreply identity, never a personal address.
