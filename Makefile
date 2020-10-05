init:
	poetry install

default: lint test

lint:
	poetry run flake8 --ignore E501 tfworker tests

format:
	poetry run black tfworker tests
	@poetry run seed-isort-config || echo "known_third_party setting changed. Please commit pyproject.toml"
	poetry run isort tfworker tests

test:
	poetry run pytest
