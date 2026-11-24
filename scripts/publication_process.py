"""Private controller transport for one immutable publication snapshot (Linux).

Standard library only: the operator launcher must not import the target runtime.
The selected release constructs and validates the snapshot with its own package.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# Linux UAPI fcntl.h values; some standalone Python builds omit the names.
F_ADD_SEALS, F_GET_SEALS, SEALS = 1033, 1034, 0x0F
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024


class SealedBytes:
    """Read-only, size-bounded bytes shared across one subprocess boundary."""

    def __init__(self, raw: bytes, *, label: str = "publication"):
        self.label = label
        if not raw or len(raw) > MAX_SNAPSHOT_BYTES:
            raise ValueError(f"{label} snapshot exceeds its size bound")
        self.hash = hashlib.sha256(raw).digest()
        self.fd = os.memfd_create(f"{label}-inputs", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        try:
            with os.fdopen(os.dup(self.fd), "wb") as stream:
                stream.write(raw)
            os.fchmod(self.fd, 0o400)
            fcntl.fcntl(self.fd, F_ADD_SEALS, SEALS)
            self.verify()
        except BaseException:
            self.close()
            raise

    def verify(self) -> None:
        if (
            fcntl.fcntl(self.fd, F_GET_SEALS) != SEALS
            or os.fstat(self.fd).st_mode & 0o222
            or hashlib.sha256(os.pread(self.fd, MAX_SNAPSHOT_BYTES + 1, 0)).digest() != self.hash
        ):
            raise ValueError(f"{self.label} snapshot changed during subprocess execution")

    def close(self) -> None:
        os.close(self.fd)


class SealedInputs(SealedBytes):
    def __init__(self, raw: bytes):
        super().__init__(raw)
        try:
            self.digest = json.loads(raw)["metadata"]["publication_inputs_digest"]
            if not isinstance(self.digest, str) or not re.fullmatch(r"[0-9a-f]{64}", self.digest):
                raise ValueError("invalid digest")
        except (ValueError, KeyError, TypeError, RecursionError) as exc:
            self.close()
            raise ValueError("invalid publication snapshot identity") from exc

    def call(
        self, runner: Callable[..., Any], command: list[str], *args: Any, **kwargs: Any
    ) -> Any:
        self.verify()
        # The snapshot owns policy bytes. Do not reopen mutable checkout inputs.
        command = list(command)
        if "--name-policy" in command:
            index = command.index("--name-policy")
            del command[index : index + 2]
        if "--publication-inputs-digest" in command:
            index = command.index("--publication-inputs-digest")
            if index + 1 >= len(command) or command[index + 1] != self.digest:
                raise ValueError("conflicting publication input digest")
        else:
            command += ["--publication-inputs-digest", self.digest]
        command += ["--publication-inputs-fd", str(self.fd)]
        try:
            return runner(command, *args, pass_fds=(self.fd,), **kwargs)
        finally:
            self.verify()


@contextmanager
def publication_process(
    repo: Path, runner: Callable[..., Any], year: int, **snapshot_kwargs: Any
) -> Iterator[Callable[..., Any]]:
    # Inspect the verified release's explicit declaration without importing it.
    # Module names are implementation details, not protocol capabilities.
    adapter = repo / "scripts/publication_legacy.py"
    declarations = (
        re.findall(r"(?m)^[ \t]*PUBLICATION_SNAPSHOT_PROTOCOL\b.*$", adapter.read_text())
        if adapter.is_file()
        else []
    )
    if not declarations:
        yield runner
        return
    if declarations != ["PUBLICATION_SNAPSHOT_PROTOCOL = 1"]:
        raise ValueError("unsupported or malformed publication snapshot protocol")
    raw = runner(
        [
            str(repo / ".venv/bin/python"),
            str(repo / "scripts/publication_legacy.py"),
            "snapshot",
            "--build-year",
            str(year),
            "--name-policy",
            str(repo / "scripts/canonical-shared-names.json"),
        ],
        cwd=repo,
        **snapshot_kwargs,
    )
    snapshot = SealedInputs(raw.encode() if isinstance(raw, str) else raw)
    try:

        def invoke(command: list[str], *args: Any, **kwargs: Any) -> Any:
            return snapshot.call(runner, command, *args, **kwargs)

        yield invoke
    finally:
        snapshot.close()
