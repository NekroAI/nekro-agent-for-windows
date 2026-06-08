# Project Rules

This project is a Windows-first Python 3.11 / PyQt6 launcher for managing a
WSL-hosted Nekro Agent runtime. Keep changes conservative and aligned with the
existing launcher architecture.

## Workflow

- Preserve user changes in the working tree. Do not reset, checkout, or delete unrelated changes.
- Use `rg`/`rg --files` for searches and `apply_patch` for manual edits.
- Verify Python changes with `uv run poe lint` when Poe is available.
- `poe lint` runs compile checks, unit tests, Pyright, lightweight static checks,
  line ending checks, and `git diff --check`.
- Keep source text CRLF by default. Shell scripts are the only LF exception.

## Python

- Target Python 3.11 and the dependencies already declared in `pyproject.toml`.
- Do not add runtime dependencies without updating packaging and explaining why the standard library or existing helpers are insufficient.
- File IO must specify an encoding for text files.
- User-facing failures should include actionable context. For WSL, Docker,
  Compose, download, update, and migration flows, include the failed action plus
  command output when available.
- Avoid broad `except Exception` in operational code unless the failure is
  intentionally non-fatal; log, emit, or surface the exception where it helps
  diagnosis.
- Keep subprocess and WSL execution behind existing helpers in
  `core/wsl/shell.py` and related mixins. Quote shell paths and values with
  `shlex.quote`.

## UI

- Reuse shared widgets and factories from `ui/widgets.py` before adding
  page-local variants.
- Use `DialogShell`, `show_confirm_dialog`, `show_choice_dialog`, and
  `show_combo_choice_dialog` for small modal dialogs.
- Use `WizardDialogBase` and `make_wizard_button` for wizard dialogs.
- Keep long-running work off the Qt UI thread. Use existing `QThread` or daemon
  worker patterns and emit UI updates through signals.
- Do not introduce new inline styles for existing roles such as wizard buttons,
  secondary buttons, segment buttons, or dialog shells; extend `ui/styles.py`
  when a reusable visual role is needed.

## Configuration And Runtime State

- Persist launcher state through `ConfigManager`; avoid direct writes to `config.json` outside that class.
- Multi-instance state must update both global compatibility fields and the
  selected instance where current behavior depends on both.
- Validate user-provided ports with `core.port_utils.validate_port_bindings`.
- Treat WSL distribution, Docker daemon, Compose project, and browser profile
  state as external state that can disappear or change while the app is running.
