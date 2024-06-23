init:
	poetry install

init-dev:
	poetry install --with dev

default: lint test

lint: init-dev
	poetry run flake8 --ignore E501,W503 tfworker tests

format: init-dev
	poetry run black tfworker tests
	@poetry run seed-isort-config || echo "known_third_party setting changed. Please commit pyproject.toml"
	poetry run isort tfworker tests

test: init-dev
	poetry run pytest -p no:warnings --disable-socket
	poetry run coverage report --fail-under=60 -m --skip-empty

ci-test: init-dev
	poetry run pytest --disable-socket --junitxml=reports/junit.xml
	poetry run coverage xml -o reports/coverage.xml

dep-test: init-dev
	poetry run pytest --disable-socket
	poetry run coverage report --fail-under=60 -m --skip-empty

clean:
	@echo "removing python temporary and build files "
	@rm -rf build dist .eggs terraform_worker.egg-info
	@find . -name *.pyc -exec rm {} \;
	@find . -name __pycache__ -type d -exec rmdir {} \;
