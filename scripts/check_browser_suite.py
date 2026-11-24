"""Run every browser acceptance command, bounded within one CI runner job."""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from ci_resources import monitor_browser

WEB_VARIANTS = {
    "public": (),
    "family": ("--family",),
    "source-preview": ("--source-preview",),
    "names-public": ("--names-preview",),
    "names-family": ("--names-preview", "--family"),
    "historical": ("--historical",),
}
VARIANTS = {
    **{
        name: (sys.executable, "scripts/check_synthetic_browser.py", name)
        for name in (*WEB_VARIANTS, "auth")
    },
    "indicators": (sys.executable, "scripts/check_person_indicators.py"),
    "prototype": ("node", "scripts/browser_scenarios/prototype.mjs", "--visual"),
}


def run_variant(name: str) -> int:
    started = time.monotonic()
    directory = Path("test-results/workshop") / name
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "checks.log").open("w") as log:
        process = subprocess.run(
            VARIANTS[name],
            stdout=log,
            stderr=subprocess.STDOUT,
            env={**os.environ, "WEB_TEST_ARTIFACT_DIR": str(directory)},
            check=False,
        )
    outcome = "PASS" if process.returncode == 0 else "FAIL"
    print(f"{outcome} {name}: {time.monotonic() - started:.1f}s", flush=True)
    print((directory / "checks.log").read_text(), flush=True)
    return process.returncode


def run_suite(names: list[str], workers: int, runner: Callable[[str], int] | None = None) -> int:
    runner = runner or run_variant
    if not names or len(names) != len(set(names)) or any(name not in VARIANTS for name in names):
        raise ValueError("Select distinct known browser variants")
    if workers not in (1, 2):
        raise ValueError("Use one or two workers within the existing runner")
    remaining = iter(names)
    failed = False
    completed = 0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            active = {executor.submit(runner, name) for name in names[:workers]}
            for _ in range(min(workers, len(names))):
                next(remaining)
            while active:
                done, active = concurrent.futures.wait(
                    active, return_when=concurrent.futures.FIRST_COMPLETED
                )
                for future in done:
                    # Exceptions also propagate as failure; never report a partial
                    # matrix green. Let running harnesses finish their cleanup.
                    failed |= future.result() != 0
                    completed += 1
                if not failed:
                    for _ in done:
                        name = next(remaining, None)
                        if name is not None:
                            active.add(executor.submit(runner, name))
    finally:
        for name in remaining:
            print(f"NOT RUN {name}: stopped after browser failure", flush=True)
    return 0 if not failed and completed == len(names) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument(
        "--variant",
        action="append",
        choices=tuple(VARIANTS),
        help="Local targeted run; default: all nine",
    )
    args = parser.parse_args()
    # The packaged binary is extracted into a shared temporary directory using
    # non-atomic writes. Complete extraction before either worker can launch it;
    # a standalone local run must not rely on an earlier CI browser step.
    subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            "import chromium from '@sparticuz/chromium'; await chromium.executablePath();",
        ],
        check=True,
    )
    with monitor_browser(Path("test-results/resources/browser.jsonl")):
        names = args.variant or list(VARIANTS)
        result = run_suite(names, args.workers)
        if result:
            # Preserve the acceptance failure if telemetry cleanup also fails.
            raise SystemExit(result)
    raise SystemExit(result)


if __name__ == "__main__":
    main()
