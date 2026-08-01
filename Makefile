### Defensive settings for make:
#     https://tech.davis-hansson.com/p/make/
SHELL:=bash
.ONESHELL:
.SHELLFLAGS:=-xeu -o pipefail -O inherit_errexit -c
.SILENT:
.DELETE_ON_ERROR:
MAKEFLAGS+=--warn-undefined-variables
MAKEFLAGS+=--no-builtin-rules

# We like colors
# From: https://coderwall.com/p/izxssa/colored-makefile-for-golang-projects
RED=`tput setaf 1`
GREEN=`tput setaf 2`
RESET=`tput sgr0`
YELLOW=`tput setaf 3`

# Python checks
UV?=uv

# installed?
ifeq (, $(shell which $(UV) ))
  $(error "UV=$(UV) not found in $(PATH)")
endif

PROJECT_FOLDER=$(shell dirname $(realpath $(firstword $(MAKEFILE_LIST))))

VENV_FOLDER=$(PROJECT_FOLDER)/.venv
BIN_FOLDER=$(VENV_FOLDER)/bin

IMAGE_NAME?=ghcr.io/simplesconsultoria/zodb-backup
IMAGE_TAG?=dev

# Build metadata baked into the image as OCI labels. VCS_REF and BUILD_DATE fall
# back to "unknown" rather than to a wrong value, e.g. in a repository with no
# commits yet or an export without git history.
VERSION=$(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' src/zodb_backup/__init__.py)
VCS_REF=$(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
BUILD_DATE=$(shell date -u +%Y-%m-%dT%H:%M:%SZ)

all: build

# Add the following 'help' target to your Makefile
# And add help text after each target name starting with '\#\#'
.PHONY: help
help: ## This help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

$(VENV_FOLDER): ## Install dependencies
	@echo "$(GREEN)==> Install environment$(RESET)"
	@uv sync

.PHONY: install
install: $(VENV_FOLDER) ## Install the project

.PHONY: clean
clean: ## Clean environment
	@echo "$(RED)==> Cleaning environment and build$(RESET)"
	rm -rf $(VENV_FOLDER) .python-version .ruff_cache .pytest_cache .mypy_cache .coverage htmlcov dist build uv.lock

############################################
# QA
############################################
.PHONY: lint
lint: $(VENV_FOLDER) ## Check code base according to our standards
	@echo "$(GREEN)==> Lint codebase$(RESET)"
	@uvx ruff@latest check --config $(PROJECT_FOLDER)/pyproject.toml
	@uvx ruff@latest format --check --config $(PROJECT_FOLDER)/pyproject.toml
	@uvx pyroma@latest -d .
	@uvx check-python-versions@latest .
	@uv run mypy

.PHONY: format
format: $(VENV_FOLDER) ## Fix code base according to our standards
	@echo "$(GREEN)==> Format codebase$(RESET)"
	@uvx ruff@latest check --select I --fix --config $(PROJECT_FOLDER)/pyproject.toml
	@uvx ruff@latest format --config $(PROJECT_FOLDER)/pyproject.toml

.PHONY: check
check: format lint ## Check and fix code base according to Plone standards

############################################
# Tests
############################################
.PHONY: test
test: $(VENV_FOLDER) ## Test the code with pytest
	@echo "🚀 Testing code: Running pytest"
	@uv run pytest -m "not integration"

.PHONY: test-integration
test-integration: $(VENV_FOLDER) docker-build ## Run the opt-in tests that need a docker compose stack
	@echo "🚀 Testing code: Running integration tests"
	@uv run pytest -m integration

.PHONY: test-coverage
test-coverage: $(VENV_FOLDER) ## Test the code with pytest and report coverage
	@echo "🚀 Testing code: Running pytest"
	@uv run pytest -m "not integration" --cov=zodb_backup --cov-report term-missing

############################################
# Container image
############################################
.PHONY: docker-build
docker-build: ## Build the container image
	@echo "$(GREEN)==> Build container image $(IMAGE_NAME):$(IMAGE_TAG) ($(VERSION))$(RESET)"
	@docker build \
		--build-arg VERSION=$(VERSION) \
		--build-arg VCS_REF=$(VCS_REF) \
		--build-arg BUILD_DATE=$(BUILD_DATE) \
		-t $(IMAGE_NAME):$(IMAGE_TAG) .

.PHONY: stack-down
stack-down: ## Tear down the integration stack and its volumes
	@echo "$(RED)==> Tearing down the test stack$(RESET)"
	@docker compose -f docker-compose.test.yml down -v

############################################
# Release
############################################
.PHONY: build
build: $(VENV_FOLDER) ## Build the sdist and wheel
	@echo "🚀 Build package"
	@rm -Rf dist
	@uv build

.PHONY: changelog
changelog: $(VENV_FOLDER) ## Display a draft of the changelog
	@echo "🚀 Display the draft for the changelog"
	@uv run towncrier --draft

.PHONY: release
release: $(VENV_FOLDER) ## Release the package to pypi.org
	@echo "🚀 Release package"
	@uv run prerelease
	@uv run release
	@rm -Rf dist
	@uv build
	@uv publish
	@uv run postrelease
