.PHONY: verify test lint fmt fmt-check typecheck mdlint live

verify: lint fmt-check typecheck test mdlint

test:
	uv run pytest

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

fmt-check:
	uv run ruff format --check .

typecheck:
	uv run pyright

mdlint:
	uv run pymarkdown --config .pymarkdown.json scan -r --respect-gitignore .

# Maintainer only: real hosts and accounts (docs/live-checklist.md). Never part of verify or CI.
live:
	uv run pytest -m live tests/live -rs
