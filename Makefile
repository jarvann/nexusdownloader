# NexusDownloader Makefile
# Cross-platform build automation

.PHONY: build clean install test help dev-setup windows-installer linux-package

# Default target
all: build

# Build executable and installer/package
build:
	@echo "Building NexusDownloader..."
	python build.py

# Build executable only (no installer)
build-exe:
	@echo "Building executable only..."
	python build.py --skip-installer

# Clean build artifacts
clean:
	@echo "Cleaning build artifacts..."
	python build.py --clean-only

# Install development dependencies
dev-setup:
	@echo "Setting up development environment..."
	pip install -r requirements-build.txt
	pip install -r requirements.txt

# Test the build
test: build
	@echo "Testing executable..."
ifeq ($(OS),Windows_NT)
	@dist/NexusDownloader/NexusDownloader.exe --help || echo "Executable test failed"
else
	@dist/NexusDownloader/NexusDownloader --help || echo "Executable test failed"
endif

# Windows-specific installer (requires NSIS)
windows-installer:
ifeq ($(OS),Windows_NT)
	@echo "Building Windows installer..."
	build_windows.bat
else
	@echo "Windows installer can only be built on Windows"
endif

# Linux-specific package
linux-package:
ifneq ($(OS),Windows_NT)
	@echo "Building Linux package..."
	./build_linux.sh
else
	@echo "Linux package can only be built on Linux"
endif

# Install from built executable
install: build
ifneq ($(OS),Windows_NT)
	@echo "Installing on Linux..."
	@if [ -f nexusdownloader-linux-x86_64.tar.gz ]; then \
		tar -xzf nexusdownloader-linux-x86_64.tar.gz; \
		cd nexusdownloader-linux-x86_64 && sudo ./install.sh; \
	else \
		echo "Linux package not found. Run 'make build' first."; \
	fi
else
	@echo "On Windows, run the installer .exe file manually"
endif

# Help
help:
	@echo "NexusDownloader Build System"
	@echo ""
	@echo "Available targets:"
	@echo "  build            - Build executable and installer/package (default)"
	@echo "  build-exe        - Build executable only (no installer)"
	@echo "  clean            - Clean build artifacts"
	@echo "  dev-setup        - Install development dependencies"
	@echo "  test             - Test the built executable"
	@echo "  windows-installer- Build Windows installer (Windows only)"
	@echo "  linux-package    - Build Linux package (Linux only)"
	@echo "  install          - Install from built files"
	@echo "  help             - Show this help message"
	@echo ""
	@echo "Examples:"
	@echo "  make build       - Full build"
	@echo "  make clean       - Clean up"
	@echo "  make dev-setup   - Setup development environment"