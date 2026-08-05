.PHONY: install dev migrate revision seed test lint typecheck up down

install:
	pip install -e ".[dev]"

dev:
	uvicorn app.main:app --reload --port 8000

migrate:
	alembic upgrade head

revision:
	alembic revision --autogenerate -m "$(m)"

seed:
	python -m scripts.seed

test:
	ENV=test pytest -q

lint:
	ruff check app scripts tests

typecheck:
	mypy app

up:
	docker compose up --build

down:
	docker compose down
