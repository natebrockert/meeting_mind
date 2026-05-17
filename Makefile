.PHONY: dev backend frontend doctor test lint

doctor:
	uv run meetingmind doctor

backend:
	uv run uvicorn app.main:app --app-dir backend --host 127.0.0.1 --reload

frontend:
	cd frontend && npm run dev

test:
	uv run pytest

lint:
	uv run ruff check backend
