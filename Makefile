IMAGE_NAME ?= gtonic/nfl-mcp-server
IMAGE_TAG ?= latest
CONTAINER_NAME ?= nfl-mcp-server
PORT ?= 9000

.DEFAULT_GOAL := help

.PHONY: help install test test-quick lint run run-dev build run-docker run-docker-detached stop-docker logs health-check clean setup ci all

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*##"}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

install: ## Install project dependencies
	pip install -r requirements.txt
	pip install -e ".[dev]"

test: ## Run unit tests
	pytest tests/ -v --cov=nfl_mcp --cov-report=term-missing

test-quick: ## Run tests without coverage
	pytest tests/ -v

lint: ## Run code linting (if available)
	python -m py_compile nfl_mcp/server.py
	python -m py_compile tests/test_server.py

run: ## Run the server locally
	python -m nfl_mcp.server

run-dev: ## Run the server in development mode with auto-reload
	python nfl_mcp/server.py

build: ## Build Docker image
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) .

run-docker: build ## Run server in Docker container
	docker run --rm --name $(CONTAINER_NAME) -p $(PORT):$(PORT) $(IMAGE_NAME):$(IMAGE_TAG)

run-docker-detached: build ## Run server in Docker container (detached)
	docker run -d --name $(CONTAINER_NAME) -p $(PORT):$(PORT) $(IMAGE_NAME):$(IMAGE_TAG)

stop-docker: ## Stop Docker container
	docker stop $(CONTAINER_NAME) || true
	docker rm $(CONTAINER_NAME) || true

logs: ## Show Docker container logs
	docker logs -f $(CONTAINER_NAME)

health-check: ## Check server health
	curl -f http://localhost:$(PORT)/health

clean: ## Clean up Docker resources
	docker stop $(CONTAINER_NAME) || true
	docker rm $(CONTAINER_NAME) || true
	docker rmi $(IMAGE_NAME):$(IMAGE_TAG) || true

setup: install test ## Complete project setup

ci: lint test build ## Run CI pipeline (test and build)

all: setup build run-docker-detached ## Full pipeline - setup, test, build and run
	sleep 5
	$(MAKE) health-check
	$(MAKE) stop-docker
