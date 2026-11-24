"""Measure CI disk allocations without reading file contents or deleting evidence."""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def snapshot(label: str) -> dict[str, object]:
    roots = [
        Path.cwd(),
        Path("/tmp"),
        Path("/dev/shm"),
        Path("/var/lib/docker"),
        Path("/var/crash"),
        Path("/var/lib/apport/coredump"),
        Path("/var/lib/systemd/coredump"),
    ]
    usage: dict[str, object] = {}
    for root in roots:
        if not root.exists():
            continue
        disk = shutil.disk_usage(root)
        stat = os.statvfs(root)
        # Allocated bytes, not apparent length (sparse files must not dominate).
        du_status: int | str
        try:
            result = subprocess.run(
                ["du", "-x", "-a", "-B1", "--max-depth=1", str(root)],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            du_status = result.returncode
            output = result.stdout
        except subprocess.TimeoutExpired:
            # An incomplete directory inventory is not an acceptance failure.
            # Retain filesystem capacity and explicitly mark the missing scan.
            du_status = "timeout"
            output = ""
        entries = []
        for line in output.splitlines():
            size, _, path = line.partition("\t")
            if size.isdecimal():
                entries.append((int(size), path))
        usage[str(root)] = {
            "free_bytes": disk.free,
            "free_inodes": stat.f_favail,
            "largest_entries": sorted(entries, reverse=True)[:15],
            "du_status": du_status,
        }
    pattern = Path("/proc/sys/kernel/core_pattern")
    return {
        "label": label,
        "time": time.time(),
        "core_limits": resource.getrlimit(resource.RLIMIT_CORE),
        "core_pattern": pattern.read_text().strip() if pattern.exists() else "unavailable",
        "usage": usage,
    }


def browser_sample(proc: Path = Path("/proc"), disk: Path = Path("/tmp")) -> dict[str, int]:
    """Count allocated unlinked browser scratch once per inode, across processes.

    du cannot see unlinked files while Chromium still holds them open. Read only
    process names and fd metadata; never read argv, environments or file bytes.
    Processes/fds may disappear between any two syscalls.
    """
    seen: set[tuple[int, int]] = set()
    allocated = count = processes = rss = 0
    for process in proc.glob("[0-9]*"):
        try:
            if "chrom" not in (process / "comm").read_text():
                continue
            processes += 1
            for line in (process / "status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    rss += int(line.split()[1]) * 1024
            for fd in (process / "fd").iterdir():
                try:
                    info = fd.stat()
                    key = (info.st_dev, info.st_ino)
                    if info.st_nlink == 0 and stat.S_ISREG(info.st_mode) and key not in seen:
                        seen.add(key)
                        allocated += info.st_blocks * 512
                        count += 1
                except OSError:
                    pass
        except OSError:
            pass
    core_bytes = core_files = 0
    for core in disk.glob("core.*"):
        try:
            info = core.lstat()
            if stat.S_ISREG(info.st_mode):
                core_files += 1
                core_bytes += info.st_blocks * 512
        except OSError:
            pass
    fs = os.statvfs(disk)
    return {
        "free_bytes": shutil.disk_usage(disk).free,
        "free_inodes": fs.f_favail,
        "browser_processes": processes,
        "browser_rss_bytes": rss,
        "unlinked_bytes": allocated,
        "unlinked_files": count,
        "core_bytes": core_bytes,
        "core_files": core_files,
    }


@contextmanager
def monitor_browser(path: Path, interval: float = 2) -> Iterator[None]:
    """Retain bounded numeric samples, including on a failed acceptance run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stopped = threading.Event()
    started = time.monotonic()
    minimum = shutil.disk_usage("/tmp").free
    peaks = {key: 0 for key in ("unlinked_bytes", "core_bytes", "browser_rss_bytes")}
    errors: list[Exception] = []
    log = path.open("w")

    def sample() -> None:
        nonlocal minimum
        result = browser_sample()
        minimum = min(minimum, result["free_bytes"])
        for key in peaks:
            peaks[key] = max(peaks[key], result[key])
        record = {"elapsed_seconds": round(time.monotonic() - started, 3), **result}
        try:
            log.write(json.dumps(record) + "\n")
            log.flush()
        except OSError:
            # Preserve the observation even when ENOSPC prevents artifact writes.
            print("Browser resource sample could not be saved: " + json.dumps(record), flush=True)
            raise

    def run() -> None:
        try:
            while not stopped.wait(interval):
                sample()
        except Exception as error:
            errors.append(error)
            stopped.set()

    thread = threading.Thread(target=run, name="browser-resource-samples", daemon=True)
    running = False
    primary_error: BaseException | None = None
    try:
        sample()
        thread.start()
        running = True
        yield
    except BaseException as error:
        primary_error = error
        raise
    finally:
        stopped.set()
        if running:
            thread.join()
            try:
                sample()
            except Exception as error:
                errors.append(error)
        try:
            log.close()
        except Exception as error:
            errors.append(error)
        print(
            "Browser resources: "
            + json.dumps({"minimum_free_bytes": minimum, "peak_allocations": peaks}),
            flush=True,
        )
        if errors:
            if primary_error is None:
                raise RuntimeError("Browser resource sampling failed") from errors[0]
            # Keep the acceptance/startup exception primary, including SystemExit
            # and KeyboardInterrupt. Telemetry failures must not hide its cause.
            for sampling_error in errors:
                primary_error.add_note(
                    f"Browser resource sampling also failed ({type(sampling_error).__name__})"
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label")
    args = parser.parse_args()
    if not args.label.replace("-", "").isalnum():
        parser.error("label must be alphanumeric with optional hyphens")
    directory = Path("test-results/resources")
    directory.mkdir(parents=True, exist_ok=True)
    result = snapshot(args.label)
    print(json.dumps(result), flush=True)
    (directory / f"{args.label}.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
