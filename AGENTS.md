# AGENT INSTRUCTIONS

## Workflow
- Use Poetry (`poetry install --with dev`)
- Run: `make test`, `make lint`, `make format`, `make typecheck`
- Use `poetry run` for all tools

## Testing
- Mirror `tfworker/` → `tests/`
- Coverage: >98% new, >90% overall
- Use `pytest-cov`
- Organize with `unittest.TestCase` classes
- Mock with `pytest-mock` or `unittest.mock`

## Typing
- Add annotations to all new modules
- Use `reveal_type()` for clarity
- Avoid mass inference tools

## AI Guidance
- Include tests unless trivial
- Use dependency injection
- Follow structure, coverage, and typing rules

## Editing Rules
- Prefer minimal diffs
- Preserve existing style
- Avoid full rewrites

## Structure
- Code: `tfworker/`
- Tests: `tests/`
- CLI: `tfworker/commands/`

## Conventions
- Test names: `test_<module>.py`
- Modules: `snake_case.py`
- No mixed-responsibility files

## Pull Requests
- Include tests
- Reference `make` targets
- Explain changes briefly
- Use imperative commit messages
