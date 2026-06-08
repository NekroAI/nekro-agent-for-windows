import ast
import compileall
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOTS = [ROOT / "main.py", ROOT / "core", ROOT / "ui", ROOT / "tests"]
SKIP_UNUSED_IMPORTS = {
    ROOT / "core" / "wsl" / "__init__.py",
    ROOT / "ui" / "pages" / "__init__.py",
}
TEXT_FILES = [
    ROOT / ".cursor" / "rules" / "project.mdc",
    ROOT / ".editorconfig",
    ROOT / ".gitattributes",
    ROOT / "AGENTS.md",
    ROOT / "pyproject.toml",
]
TEXT_DIRS = [ROOT / "core", ROOT / "ui", ROOT / "tests", ROOT / "scripts"]


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


def iter_python_files():
    for root in PYTHON_ROOTS:
        if root.is_file():
            yield root
        elif root.exists():
            yield from sorted(root.rglob("*.py"))


def iter_text_files():
    seen = set()
    for path in TEXT_FILES:
        if path.exists():
            seen.add(path)
            yield path
    for root in TEXT_DIRS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in {".py", ".md", ".mdc", ".toml"}:
                if path not in seen:
                    seen.add(path)
                    yield path


def run_step(name, func):
    print(f"==> {name}")
    errors = func()
    if errors:
        for error in errors:
            print(error)
        return False
    print("ok")
    return True


def check_compileall():
    errors = []
    for root in PYTHON_ROOTS:
        if root.is_file():
            ok = compileall.compile_file(str(root), quiet=1)
        elif root.exists():
            ok = compileall.compile_dir(str(root), quiet=1)
        else:
            ok = True
        if not ok:
            errors.append(f"{rel(root)}: compile failed")
    return errors


def check_unittest():
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    if proc.returncode != 0:
        return ["unittest discover failed"]
    return []


def check_pyright():
    proc = subprocess.run(
        [sys.executable, "-m", "pyright"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    if proc.returncode != 0:
        return ["pyright failed"]
    return []


def check_unused_imports():
    errors = []
    for path in iter_python_files():
        if path in SKIP_UNUSED_IMPORTS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    if name not in used:
                        errors.append(f"{rel(path)}:{node.lineno}: unused import {name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module == "__future__":
                    continue
                for alias in node.names:
                    name = alias.asname or alias.name
                    if name != "*" and name not in used:
                        errors.append(f"{rel(path)}:{node.lineno}: unused import {name}")
    return errors


def check_dangerous_calls():
    errors = []
    for path in iter_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec"}:
                errors.append(f"{rel(path)}:{node.lineno}: avoid {node.func.id}()")
            for keyword in node.keywords:
                if (
                    keyword.arg == "shell"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    errors.append(f"{rel(path)}:{node.lineno}: avoid subprocess shell=True")
    return errors


def check_line_endings_and_whitespace():
    errors = []
    for path in iter_text_files():
        data = path.read_bytes()
        if data and not data.endswith((b"\n", b"\r\n")):
            errors.append(f"{rel(path)}: missing final newline")

        if path.suffix == ".sh":
            if b"\r\n" in data:
                errors.append(f"{rel(path)}: shell scripts must use LF")
        else:
            for index, byte in enumerate(data):
                if byte == 0x0A and (index == 0 or data[index - 1] != 0x0D):
                    errors.append(f"{rel(path)}: use CRLF line endings")
                    break

        text = data.decode("utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.rstrip(" \t") != line:
                errors.append(f"{rel(path)}:{lineno}: trailing whitespace")
    return errors


def check_git_diff():
    proc = subprocess.run(
        ["git", "diff", "--check"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return [output.strip() or "git diff --check failed"]
    return []


def main():
    steps = [
        ("compileall", check_compileall),
        ("unittest", check_unittest),
        ("pyright", check_pyright),
        ("unused imports", check_unused_imports),
        ("dangerous calls", check_dangerous_calls),
        ("line endings and whitespace", check_line_endings_and_whitespace),
        ("git diff --check", check_git_diff),
    ]
    ok = True
    for name, func in steps:
        ok = run_step(name, func) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
