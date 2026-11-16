.PHONY: dev test lint fmt clean

dev:
	uv run python -m $(shell basename $(CURDIR))

test:
	uv run pytest

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	rm -rf .pytest_cache .ruff_cache .mypy_cache
