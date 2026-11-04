"""Run the same AI quality checks locally, in pre-push, and in CI."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AI_ROOT = ROOT / "ai"


def run(*args: str) -> None:
    print("+ " + " ".join(args), flush=True)
    subprocess.run([sys.executable, "-m", *args], cwd=AI_ROOT, check=True)


def main() -> None:
    run("ruff", "format", "--check", ".", "../scripts/check_ai.py")
    run("ruff", "check", ".", "../scripts/check_ai.py")
    run("mypy", "../scripts/check_ai.py")

    app = AI_ROOT / "app"
    has_app = app.exists() and any(app.rglob("*.py"))
    tests = AI_ROOT / "tests"
    has_tests = tests.exists() and any(
        path.name.startswith("test_") or path.name.endswith("_test.py")
        for path in tests.rglob("*.py")
    )
    if has_app:
        run("mypy", "app")
    if has_tests:
        # No selected unit tests is an error (pytest exit code 5).
        run("pytest", "-m", "unit and not integration and not llm and not rag")
    elif has_app:
        raise SystemExit("AI source exists but no tests exist. Add marked unit tests.")
    else:
        print("BOOTSTRAP: no AI source/tests yet; application checks were not run.")


if __name__ == "__main__":
    main()
