# AGENT INSTRUCTIONS

## Testing
- This repository uses Poetry.
- Install dependencies with `poetry install --with dev`.
- Run tests using `poetry run pytest` or `make test`.

## Linting
- Use `poetry run flake8` or `make lint` to lint code.

## Formatting
- Use `make format` to format the code with Black and isort (or run `poetry run black` and `poetry run isort`).

Always run the tests and linting after making changes and ensure the code is formatted before committing.
