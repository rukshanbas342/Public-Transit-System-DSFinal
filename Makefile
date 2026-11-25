# Shortcuts. Run `make help` for the list.
DEV = docker compose -f docker-compose.yml -f docker-compose.dev.yml
.PHONY: help up up-fast down reset logs infra psql sim-local test dashboard

help:            ## show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

up:              ## start everything; dashboard at http://localhost:8000
	docker compose up --build

up-fast:         ## rush hour at 4 sim-minutes per second (whole day in ~4.5 min)
	SIM_SCENARIO=rush_hour SIM_SPEED=0.25 docker compose up --build

down:            ## stop all containers (keeps data)
	docker compose down

reset:           ## stop and delete all data (database + queues)
	docker compose down -v

logs:            ## follow logs of all services
	docker compose logs -f

infra:           ## dev: only postgres + rabbitmq + migrate, with ports 5432/5672 published
	$(DEV) up --build postgres rabbitmq migrate

psql:            ## open psql inside the database container
	docker compose exec postgres psql -U transit

sim-local:       ## run a fast simulation with no infrastructure and print metrics
	python -m transit.simulator --scenario quick_test --sink none --schedule fixed

test:            ## run unit tests
	python -m pytest -q

dashboard:       ## dev: run the dashboard with hot reload on :5173 (API must be on :8000)
	cd dashboard && npm install && npm run dev
