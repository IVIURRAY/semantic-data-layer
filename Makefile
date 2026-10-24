.PHONY: up down demo test logs psql

up:            ## build and start Postgres, seed all stores, start the API on :8000
	docker compose up -d --build --wait
	@echo "API: http://localhost:8000/docs"

down:          ## stop everything and delete the seeded data
	docker compose down -v

demo:          ## run the walkthrough against the running estate
	docker compose run --rm api python examples/walkthrough.py

test:          ## run the test suite inside the container
	docker compose run --rm api pytest -q -p no:warnings

logs:
	docker compose logs -f api

psql:          ## open a psql shell on the OMS
	docker compose exec oms-db psql -U semlayer -d oms
