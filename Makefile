.PHONY: help sync lint format test ci check run

help:
	@echo "Available commands:"
	@echo "  make sync    - Install and sync dependencies with uv"
	@echo "  make lint    - Run ruff linter and mypy type checks"
	@echo "  make format  - Auto-format code with ruff"
	@echo "  make test    - Run pytest test suite"
	@echo "  make ci      - Non-mutating lint + typecheck + test (for GitHub Actions)"
	@echo "  make check   - Format, then lint and test"
	@echo "  make run     - Run the in-repo CLI demo"

sync:
	uv sync --all-extras

lint:
	uv run ruff check src tests resources/templates
	uv run mypy src

format:
	uv run ruff check --fix src tests resources/templates
	uv run ruff format src tests resources/templates

test:
	uv run pytest tests

ci:
	uv run ruff format --check src tests resources/templates
	uv run ruff check src tests resources/templates
	uv run mypy src
	uv run pytest tests

check: format lint test

run:
	uv run python examples/01_cli_demo.py
