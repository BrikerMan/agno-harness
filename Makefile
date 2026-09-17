.PHONY: help sync lint format test check run

help:
	@echo "Available commands:"
	@echo "  make sync    - Install and sync dependencies with uv"
	@echo "  make lint    - Run ruff linter and mypy type checks"
	@echo "  make format  - Auto-format code with ruff"
	@echo "  make test    - Run pytest test suite"
	@echo "  make check   - Run format, lint, and test"

sync:
	uv sync --all-extras

lint:
	uv run ruff check src tests
	uv run mypy src

format:
	uv run ruff check --fix src tests
	uv run ruff format src tests

test:
	uv run pytest tests

check: format lint test
