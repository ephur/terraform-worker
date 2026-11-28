init:
	uv sync --no-default-groups

init-dev:
	uv sync

default: lint test

lint: init-dev
	uv run flake8 --ignore E203,E501,W503 tfworker tests

format: init-dev
	uv run black tfworker tests
	@uv run seed-isort-config || echo "known_third_party setting changed. Please commit .isort.cfg"
	uv run isort tfworker tests

test: init-dev
	uv run pytest -p no:warnings --disable-socket
	uv run coverage report

ci-test: init-dev
	uv run pytest --disable-socket --junitxml=reports/junit.xml
	uv run coverage xml -o reports/coverage.xml

dep-test: init-dev
	uv run pytest --disable-socket
	uv run coverage report

docs: init-dev
	uv run python scripts/gen_reference.py

docs-check: init-dev
	uv run python scripts/gen_reference.py --check

typecheck: init-dev
	uv run mypy -p tfworker

typecheck-report: init-dev
	-uv run mypy tfworker | tee mypy-report.txt

clean:
	@echo "removing python temporary and build files "
	@rm -rf build dist .eggs terraform_worker.egg-info
	@find . -name *.pyc -exec rm {} \;
	@find . -name __pycache__ -type d -exec rmdir {} \;
