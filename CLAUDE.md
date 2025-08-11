# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Virtual Environment (ALWAYS ACTIVATE FIRST!)
**⚠️ CRITICAL: Before running ANY Python commands, tests, or module commands, ALWAYS activate the virtual environment:**
```bash
source ~/.venvs/uvrepin/bin/activate
```

### Installation and Setup
- `uv sync --all-packages --all-extras` - Install all dependencies and packages
- `uv sync` - Standard dependency installation
- `source ~/.venvs/uvrepin/bin/activate && uv pip install -e .` - Install the uvrepin package in editable mode for local testing

### Testing

**IMPORTANT: Virtual Environment Required**
Before running ANY Python commands or tests, you MUST activate the virtual environment:
```bash
source ~/.venvs/uvrepin/bin/activate
```

Always use this pattern for running tests:
```bash
source ~/.venvs/uvrepin/bin/activate && pytest {module}/tests
```

Examples:
- `source ~/.venvs/uvrepin/bin/activate && pytest` - Run tests for capi module


### Linting and Type Checking

**IMPORTANT: Virtual Environment Required**
Before running ANY linting or type checking commands, you MUST activate the virtual environment:
```bash
source ~/.venvs/uvrepin/bin/activate
```

**Linting with Ruff:**
- `ruff check` or for a specific module: `ruff check cettings`

Note that our .ruff.toml files exclude the tests folders. If we want to make sure the test folders are fine: `ruff check --no-force-exclude`

- Checking that we are not missing any imports: `ruff check --no-force-exclude --select F821`

**Type Checking with Pyright:**
Always use this pattern for type checking:
```bash
source ~/.venvs/uvrepin/bin/activate && pyright {file_path}
```

Examples:
- `source ~/.venvs/uvrepin/bin/activate && pyright .` - Type check entire repo

### Version Management
- `bumpversion {patch|minor|major}` - Bump version and create git tag for releases
