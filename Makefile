UV ?= uv

.PHONY: help bootstrap test lint format run image up contract compare

help:            ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

bootstrap:       ## Install the environment (uv sync)
	$(UV) sync

test:            ## Run the test suite
	$(UV) run pytest tests/

lint:            ## ruff check + ruff format --check + mypy strict
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy src/ga_mock

format:          ## Format and fix what can be fixed
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

run:             ## Start the mock locally, control plane enabled
	GA_MOCK_ADMIN_ENABLED=true $(UV) run python -m ga_mock

image:           ## Build the local docker image
	docker build -t ga-mock:dev .

up:              ## docker compose up --build
	docker compose up --build

contract:        ## Regenerate contracts/ga4-data.openapi.yaml from the app
	$(UV) run python -c "import yaml, ga_mock as m; \
open('contracts/ga4-data.openapi.yaml','w').write(yaml.safe_dump(m.contract_openapi(), sort_keys=False, allow_unicode=True))"
	@echo "✓ contract regenerated — REVIEW the diff: a shape change is a contract change for consumers"

compare:         ## Replay the mock against a REAL property (GA_REAL_SA, GA_REAL_PROPERTY)
	@test -n "$$GA_REAL_SA" || { echo "GA_REAL_SA=/path/to/sa.json required"; exit 2; }
	@test -n "$$GA_REAL_PROPERTY" || { echo "GA_REAL_PROPERTY=<id> required"; exit 2; }
	$(UV) run python scripts/compare_real.py $(ARGS)
