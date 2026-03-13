.PHONY: help install test clean lint format coverage run-client run-server example docs

help:
	@echo "STORM Protocol - Makefile commands"
	@echo ""
	@echo "Setup:"
	@echo "  make install      - Install dependencies"
	@echo ""
	@echo "Development:"
	@echo "  make lint         - Run linter (pylint)"
	@echo "  make format       - Format code (black)"
	@echo "  make test         - Run tests"
	@echo "  make coverage     - Run tests with coverage report"
	@echo ""
	@echo "Running:"
	@echo "  make run-client   - Run STORM client"
	@echo "  make run-server   - Run STORM server"
	@echo "  make example      - Run local example"
	@echo ""
	@echo "Maintenance:"
	@echo "  make clean        - Remove build artifacts and caches"
	@echo "  make docs         - Build documentation"

install:
	pip install -r requirements.txt

lint:
	pylint storm_*.py

format:
	black storm_*.py test_storm.py example_local_test.py

test:
	pytest test_storm.py -v

coverage:
	pytest test_storm.py --cov=. --cov-report=html
	@echo "Coverage report: htmlcov/index.html"

run-client:
	python storm_client.py

run-server:
	python storm_server.py

example:
	python example_local_test.py

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name ".coverage" -delete
	rm -rf .pytest_cache/ htmlcov/ build/ dist/ *.egg-info
	find . -type f -name ".DS_Store" -delete

docs:
	@echo "Documentation files:"
	@echo "  README.md - Overall project documentation"
	@echo "  DEVELOPER.md - Developer quick reference"
	@echo "  config.py - Configuration template"
