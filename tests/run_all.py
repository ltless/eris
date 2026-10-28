"""Every suite, in one command: python tests/run_all.py

The older suites are plain scripts that print `ok <name>` and exit non-zero on
failure; the newer ones are unittest classes. This runs both and reports one
total, so a change that breaks something in either style is caught the same
way.

    .venv\\Scripts\\python.exe tests/run_all.py
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Scripts that check themselves and exit non-zero on failure.
SCRIPTS = [
    "test_tools.py",
    "test_tool_loop.py",
    "test_media.py",
    "test_dialogs.py",
]

# unittest modules.
MODULES = [
    "test_runtime.py",
    "test_api.py",
]


def run(args):
    return subprocess.run([sys.executable, *args], cwd=str(HERE.parent),
                          capture_output=True, text=True)


def main():
    failures = []
    for name in SCRIPTS:
        got = run([str(HERE / name)])
        ok = got.returncode == 0
        tail = (got.stdout or got.stderr).strip().splitlines()[-1:] or [""]
        print(f"{'ok  ' if ok else 'FAIL'} {name}  ({tail[0]})")
        if not ok:
            failures.append(name)
            print(got.stdout[-4000:])
            print(got.stderr[-4000:])

    for name in MODULES:
        got = run(["-m", "unittest", f"tests.{name[:-3]}", "-v"])
        ok = got.returncode == 0
        summary = [
            line for line in (got.stderr or "").splitlines()
            if line.startswith(("Ran ", "OK", "FAILED"))
        ]
        print(f"{'ok  ' if ok else 'FAIL'} {name}  ({'; '.join(summary)})")
        if not ok:
            failures.append(name)
            print(got.stderr[-8000:])

    print()
    if failures:
        print(f"{len(failures)} suite(s) failed: {', '.join(failures)}")
        return 1
    print(f"all {len(SCRIPTS) + len(MODULES)} suites passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())