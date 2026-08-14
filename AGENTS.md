# Repository Guidelines

## Project Structure & Module Organization

This repository collects small, reusable developer tools, scripts, snippets, and utilities. It is currently intentionally minimal: `README.md` describes the project and `LICENSE` contains licensing terms. Add each new tool in a focused top-level directory, such as `scripts/format-json/` or `tools/git-helpers/`; keep its source, documentation, and supporting assets together. Put shared documentation in `docs/` only when it serves more than one tool, and place tests beside the tool in `tests/` or use the language's conventional test layout.

## Build, Test, and Development Commands

No project-wide build, package manager, formatter, or test runner is configured yet. A new tool must document its local workflow in its directory or the root README. Prefer a discoverable command such as:

```bash
make test        # run all checks, if a Makefile is added
npm test         # run JavaScript/TypeScript tests
pytest           # run Python tests
```

Do not introduce a root-level dependency or build system solely for one isolated utility unless it benefits multiple tools.

## Coding Style & Naming Conventions

Follow the formatter and idioms native to the language used. Use 2 spaces for YAML, JSON, Markdown nested lists, and JavaScript/TypeScript; use 4 spaces for Python. Name directories and executable scripts with lowercase kebab-case (`check-links.sh`); use language-standard names for source files, tests, and public APIs. Keep scripts small, portable, and explicit about required environment variables and external commands.

## Testing Guidelines

Add automated tests for behavior that can regress, including error paths and representative command-line input. Use the ecosystem's standard test runner and name tests after the behavior under test, for example `test_rejects_empty_input` or `parseConfig_returnsDefaults`. Run the tool's formatter, linter, and tests before opening a pull request.

## Commit & Pull Request Guidelines

The repository history currently contains only the initial commit, so no established commit convention exists. Use concise imperative subjects, such as `Add JSON formatting helper`. Keep commits focused. Pull requests should explain the tool's purpose, usage, validation performed, and any dependencies or configuration; include example output or screenshots when the change has a user-facing interface.

## Security & Configuration

Never commit credentials, tokens, or private configuration. Document configuration with safe example values (for example, `.env.example`) and validate required inputs before running external commands or modifying files.
