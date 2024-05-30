.PHONY: init quiet-init default lint format test dep-test clean

# the first target is the default target, so keep it at the top
# this will run the format, lint, and test targets make is smart enough to know to only
# run each target once, even if they are listed multiple times
default: format lint test

# Install dependencies
init:
	poetry install

# Install dependencies without output
quiet-init:
	@poetry install --quiet

# Lint code with flake8
lint: quiet-init
	poetry run flake8 --ignore E501,W503 tfworker tests

# Format code with black and isort
format: quiet-init
	poetry run black tfworker tests
	@poetry run seed-isort-config || echo "known_third_party setting changed. Please commit pyproject.toml"
	poetry run isort tfworker tests

# Run tests with no deprecation warnings, network sockets disabled, and fail if coverage is below 60%
test: quiet-init
	poetry run pytest -p no:warnings --disable-socket
	poetry run coverage report --fail-under=60 -m --skip-empty

# Run tests with HTML coverage report
test-html: quiet-init
	poetry run pytest -p no:warnings --disable-socket
	poetry run coverage html -d coverage --skip-empty
	open coverage/index.html

# Run tests with deprecation warnings, network sockets disabled, and fail if coverage is below 60%
dep-test: quiet-init
	poetry run pytest --disable-socket
	poetry run coverage report --fail-under=60 -m --skip-empty

# Clean up build files
clean:
	rm -rf build dist .eggs terraform_worker.egg-info
	find . -name *.pyc -exec rm {} \;
