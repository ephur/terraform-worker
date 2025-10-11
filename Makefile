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

test: init-dev go-build go-test
	poetry run pytest -p no:warnings --disable-socket
	poetry run coverage report --fail-under=60 -m --skip-empty

ci-test: init-dev go-test
	poetry run pytest --disable-socket --junitxml=reports/junit.xml
	poetry run coverage xml -o reports/coverage.xml

dep-test: init-dev
	poetry run pytest --disable-socket
	poetry run coverage report --fail-under=60 -m --skip-empty

typecheck: init-dev
	poetry run mypy -p tfworker

typecheck-report: init-dev
	-poetry run mypy tfworker | tee mypy-report.txt

clean:
	@echo "removing python temporary and build files "
	@rm -rf build dist .eggs terraform_worker.egg-info
	@find . -name *.pyc -exec rm {} \;
	@find . -name __pycache__ -type d -exec rmdir {} \;
	@rm -f tfworker-hcl2json

# --- Go integration ---

.PHONY: go-build go-test

go-build:
	@if which go >/dev/null 2>&1; then \
	  if [ ! -f tools/hcl2json/go.sum ]; then \
	    echo "[go-build] Missing tools/hcl2json/go.sum"; \
	    echo "           Run: (cd tools/hcl2json && go mod tidy) and commit go.sum"; \
	    echo "           Skipping Go build"; \
	  else \
	    echo "Building Go HCL helper..."; \
	    (cd tools/hcl2json && go build -o ../../tfworker-hcl2json); \
	  fi; \
	else \
	  echo "Go not installed, skipping go-build"; \
	fi

go-test:
	@if which go >/dev/null 2>&1; then \
	  if [ ! -f tools/hcl2json/go.sum ]; then \
	    echo "[go-test] Missing tools/hcl2json/go.sum"; \
	    echo "          Run: (cd tools/hcl2json && go mod tidy) and commit go.sum"; \
	    echo "          Skipping Go tests"; \
	  else \
	    echo "Running Go tests..."; \
	    (cd tools/hcl2json && go test ./...); \
	  fi; \
	else \
	  echo "Go not installed, skipping go-test"; \
	fi
