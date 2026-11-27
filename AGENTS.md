# Repository Guidelines

## Project Structure & Module Organization
- Source code lives in `tfworker/`; CLI entrypoints in `tfworker/commands/`.
- Tests mirror the source tree in `tests/` (e.g., `tfworker/foo/bar.py` → `tests/foo/test_bar.py`).
- Keep modules single‑purpose, use `snake_case.py` filenames, and avoid mixed responsibilities.

## Build, Test, and Development Commands
- Install tooling: `uv sync` (installs the `dev` dependency group by default)
- Run tests: `make test` (uses `pytest` with coverage via `pytest-cov`).
- Lint: `make lint`
- Format: `make format`
- Type check: `make typecheck`
- Use `uv run <tool>` when invoking tools directly (e.g., `uv run pytest -k my_test`).

## Coding Style & Naming Conventions
- Python with 4‑space indents and explicit type hints on public APIs.
- Naming: `snake_case` for modules/functions, `PascalCase` for classes, `UPPER_SNAKE` for constants.
- Follow the existing style; prefer minimal diffs over rewrites. Keep functions focused and small.

## Testing Guidelines
- Frameworks: `pytest` organized with `unittest.TestCase` classes; mock using `pytest-mock` or `unittest.mock`.
- Coverage: >98% for new/changed code. The overall floor is `fail_under` in `[tool.coverage.report]` (`pyproject.toml`), enforced by `make test` and CI; raise it when coverage goes up, never lower it.
- Structure: mirror `tfworker/` in `tests/`; test files named `test_<module>.py`.
- Run locally with `make test` or `uv run pytest`. Write fast, isolated tests and use dependency injection for seams.

## Typing
- Add annotations to all new modules and public functions; keep them precise.
- Use `reveal_type()` to clarify uncertain types during development when helpful.

## Commit & Pull Request Guidelines
- Commits: imperative mood (“Add X”, not “Added X”); concise subject, descriptive body when needed.
- PRs must include tests, reference relevant `make` targets run (test/lint/format/typecheck), and briefly explain the rationale and scope. Link issues when applicable.
- Document notable design decisions or trade‑offs in the PR description.

## Security & Configuration Tips
- Never commit secrets or credentials; prefer environment variables for configuration.
- Follow `.gitignore`; sanitize logs and error messages in tests and code.
