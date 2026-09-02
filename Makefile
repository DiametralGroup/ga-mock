UV ?= uv

.PHONY: help bootstrap test lint format run image up contract compare

help:            ## Affiche cette aide
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

bootstrap:       ## Installe l'environnement (uv sync)
	$(UV) sync

test:            ## Lance la suite de tests
	$(UV) run pytest tests/

lint:            ## ruff check + ruff format --check + mypy strict
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy src/ga_mock

format:          ## Formate et corrige ce qui peut l'être
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

run:             ## Démarre le mock en local, plan de contrôle activé
	GA_MOCK_ADMIN_ENABLED=true $(UV) run python -m ga_mock

image:           ## Construit l'image docker locale
	docker build -t ga-mock:dev .

up:              ## docker compose up --build
	docker compose up --build

contract:        ## Régénère contracts/ga4-data.openapi.yaml depuis l'app
	$(UV) run python -c "import yaml, ga_mock as m; \
open('contracts/ga4-data.openapi.yaml','w').write(yaml.safe_dump(m.contract_openapi(), sort_keys=False, allow_unicode=True))"
	@echo "✓ contrat régénéré — RELIRE le diff : un changement de forme est un changement de contrat pour les consommateurs"

compare:         ## Rejoue le mock contre une VRAIE propriete (GA_REAL_SA, GA_REAL_PROPERTY)
	@test -n "$$GA_REAL_SA" || { echo "GA_REAL_SA=/chemin/sa.json requis"; exit 2; }
	@test -n "$$GA_REAL_PROPERTY" || { echo "GA_REAL_PROPERTY=<id> requis"; exit 2; }
	$(UV) run python scripts/compare_real.py $(ARGS)
