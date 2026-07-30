PKG := datenkatalog_attribute_extractor
TOOLS := PYTHONPATH=src uv run

.PHONY: install
install: ## Install the virtual environment
	@echo "🚀 Creating virtual environment using uv"
	@uv sync --all-groups

.PHONY: check
check: ## Run code quality tools
	@echo "🚀 Checking lock file consistency with 'pyproject.toml'"
	@uv lock --locked
	@echo "🚀 Formatting code: Running ruff format"
	@uv run ruff format
	@echo "🚀 Linting code: Running ruff check"
	@uv run ruff check --fix
	@echo "🚀 Static type checking: Running ty"
	@uv run ty check ./src/$(PKG)

.PHONY: test
test: ## Run the unit tests
	@echo "🚀 Testing code: Running pytest"
	@$(TOOLS) python -m pytest tests/unit

.PHONY: test-integration
test-integration: ## Run the integration tests against a live LLM (needs .env and a running vLLM)
	@echo "🚀 Running integration tests"
	@$(TOOLS) --env-file .env python -m pytest tests/integration -v

.PHONY: dev
dev: ## Run the API in development mode
	@echo "🚀 Starting the API on http://localhost:8000"
	@$(TOOLS) --env-file .env uvicorn $(PKG).app:app --reload --port 8000

.PHONY: dev-ui
dev-ui: ## Run the Streamlit UI in development mode
	@echo "🚀 Starting the UI on http://localhost:8501"
	@$(TOOLS) --env-file .env streamlit run src/$(PKG)/ui/streamlit_app.py

.PHONY: eval
eval: ## Evaluate extraction quality against the labelled cases in evals/cases
	@echo "🚀 Running the extraction eval"
	@$(TOOLS) --env-file .env python -m $(PKG)_tools.run_extraction_eval

.PHONY: bootstrap-case
bootstrap-case: ## Generate an eval case skeleton from a PDF (PDF=path/to/file.pdf)
	@$(TOOLS) python -m $(PKG)_tools.bootstrap_case $(PDF)

.PHONY: env-example
env-example: ## Regenerate .env.example from the AppConfig model
	@$(TOOLS) generate-env-example $(PKG).utils.app_config AppConfig -o .env.example

.PHONY: docker-build
docker-build: ## Build the application image
	@echo "🐳 Building the image"
	@docker compose -f docker-compose.dev.yml build

.PHONY: docker-up
docker-up: ## Start the full dev stack (vLLM, API, UI)
	@echo "🐳 Starting docker compose"
	@docker compose -f docker-compose.dev.yml up -d

.PHONY: docker-down
docker-down: ## Stop the dev stack
	@echo "🐳 Stopping docker compose"
	@docker compose -f docker-compose.dev.yml down

.PHONY: docker-logs
docker-logs: ## Follow the dev stack logs
	@docker compose -f docker-compose.dev.yml logs -f

.PHONY: docker-lint
docker-lint: ## Lint the Dockerfile with hadolint
	@docker run --rm -i hadolint/hadolint < Dockerfile

.PHONY: help
help:
	@uv run python -c "import re; \
	[[print(f'\033[36m{m[0]:<20}\033[0m {m[1]}') for m in re.findall(r'^([a-zA-Z_-]+):.*?## (.*)$$', open(makefile).read(), re.M)] for makefile in ('$(MAKEFILE_LIST)').strip().split()]"

.DEFAULT_GOAL := help
