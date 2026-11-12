"""Single-writer ownership and conservative pending snapshots; no network I/O."""

import hashlib
import json
import os
import stat
from pathlib import Path

from config import is_valid_username


class ResultsBusyError(RuntimeError):
    pass


class RecoveryError(ValueError):
    pass


def _check_regular(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RecoveryError(
            "Results contain a linked or non-regular file. Preserve all files and review their "
            "paths before retrying in a separate results directory; do not overwrite recovery artifacts."
        )


class ResultsLock:
    """Keep a persistent lock inode; existence is never evidence of ownership."""

    def __init__(self, directory, owner=None):
        self.directory = Path(directory).resolve()
        self.owner, self.file = owner, None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / ".rovalid.lock"
        _check_regular(path)
        file = path.open("a+b")
        try:
            file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            elif os.name == "posix":
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                raise RuntimeError("Results locking is unsupported on this OS; no writes permitted")
            for name in ("hits.txt", "unresolved.txt", "unresolved.tmp", "unresolved.next"):
                _check_regular(self.directory / name)
        except OSError as exc:
            file.close()
            raise ResultsBusyError(
                "Cannot acquire exclusive results ownership. Another RoValid writer may be active; "
                "stop it or choose a different results directory. Do not delete .rovalid.lock."
            ) from exc
        except BaseException:
            file.close()
            raise
        self.file = file
        return self

    def verify(self, directory, owner):
        if self.file is None or self.file.closed or self.owner is not owner or self.directory != Path(directory).resolve():
            raise RuntimeError("Invalid results ownership context")

    def __exit__(self, *args):
        # Closing releases OS ownership even after an exception. Never unlink the
        # inode: replacing/deleting a lockfile could split cooperating writers.
        self.file.close()
        self.file = None


def sync_file(file):
    file.flush()
    os.fsync(file.fileno())


def _sync_directory(directory):
    if os.name == "posix":
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    # Windows fsync commits file data; Python provides no portable equivalent
    # for directory-entry durability. Atomic replace is not a power-loss proof.


_PREFIX = b"# RoValid pending v1 "


class PendingSnapshots:
    """One checked intent plus a plain compatible snapshot, under ResultsLock.

    unresolved.tmp: bounded JSON header (count/hash/base hash), then plain names.
    unresolved.next: staging copy for atomic replacement of unresolved.txt.
    A complete intent is retained after replacement and is reusable only when
    its target matches the current file. No timestamps decide which file wins.
    """

    def __init__(self, directory):
        self.directory = directory
        self.current = directory / "unresolved.txt"
        self.intent = directory / "unresolved.tmp"
        self.next = directory / "unresolved.next"
        self._known = False
        self.has_checked_intent = False

    def _error(self, reason):
        return RecoveryError(
            f"Pending recovery refused: {reason}. Preserve hits.txt, unresolved.txt, unresolved.tmp "
            "and unresolved.next; back them up, compare against the original inputs, and manually "
            "reconcile a complete pending list in a separate results directory before retrying. "
            "Do not delete recovery artifacts just to bypass this check."
        )

    def _scan(self, path, *, intent=False):
        try:
            file = path.open("rb")
        except FileNotFoundError:
            return None
        with file:
            first = file.readline(4097)
            header = None
            if intent and first.startswith(_PREFIX):
                try:
                    header = json.loads(first[len(_PREFIX):])
                    if (not first.endswith(b"\n") or type(header) is not dict
                            or set(header) != {"count", "digest", "base"}
                            or type(header["count"]) is not int or header["count"] < 0
                            or any(not isinstance(v, str) or len(v) != 64
                                   or any(c not in "0123456789abcdef" for c in v)
                                   for v in (header["digest"],)
                                   + (() if header["base"] is None else (header["base"],)))):
                        raise ValueError()
                except (ValueError, TypeError, KeyError, RecursionError):
                    raise self._error("invalid temporary snapshot header") from None
                first = file.readline(4097)
            digest, count, last = hashlib.sha256(), 0, b""
            line = first
            while line:
                if len(line) > 4096:
                    raise self._error(f"oversized record in {path.name}")
                digest.update(line)
                try:
                    name = line.decode("utf-8").strip()
                except UnicodeDecodeError:
                    if intent or path == self.next:
                        raise self._error(f"invalid UTF-8 in {path.name}") from None
                    raise
                if name:
                    if not is_valid_username(name):
                        raise self._error(f"invalid record in {path.name}")
                    count += 1
                if header is not None and (not name or line.rstrip(b"\n") != name.encode("utf-8")):
                    raise self._error("noncanonical temporary snapshot")
                last = line
                line = file.readline(4097)
            result = digest.hexdigest()
            if header is not None:
                if result != header["digest"] or count != header["count"]:
                    raise self._error("truncated or altered temporary snapshot")
            elif intent and last and not last.endswith(b"\n"):
                raise self._error("legacy temporary snapshot lacks a complete final record")
            return result, header

    def _copy_intent(self):
        with self.intent.open("rb") as source, self.next.open("xb") as target:
            first = source.readline(4097)
            if not first.startswith(_PREFIX):
                target.write(first)
            while block := source.read(1024 * 1024):
                target.write(block)
            sync_file(target)

    def _promote(self, digest):
        staged = self._scan(self.next)
        if staged is None:
            self._copy_intent()
            staged = self._scan(self.next)
            if staged[0] != digest:
                raise self._error("staging copy failed integrity validation")
        elif staged[0] != digest:
            raise self._error("staging copy conflicts with the temporary snapshot")
        # Also sync a complete staging copy inherited from a terminated process.
        with self.next.open("r+b") as file:
            sync_file(file)
        self.next.replace(self.current)
        _sync_directory(self.directory)

    def load(self):
        self._known = False
        self.has_checked_intent = False
        for name in ("hits.txt", "unresolved.txt", "unresolved.tmp", "unresolved.next"):
            _check_regular(self.directory / name)
        current, intent = self._scan(self.current), self._scan(self.intent, intent=True)
        staged = self._scan(self.next)
        self._base = None if current is None else current[0]
        if intent is None:
            if staged is not None:
                raise self._error("staging copy has no verifiable temporary snapshot")
        else:
            digest, header = intent
            if header is None:
                if current is not None and digest != current[0]:
                    raise self._error("conflicting legacy snapshots have no ordering evidence")
            elif digest != self._base and header["base"] != self._base:
                raise self._error("temporary snapshot matches neither current content nor its recorded base")
            if staged is not None and staged[0] != digest:
                raise self._error("staging copy conflicts with the temporary snapshot")
            if digest != self._base or staged is not None:
                self._promote(digest)
            self._base = digest
            self.has_checked_intent = header is not None
        self._known = True
        return self._names()

    def _names(self):
        try:
            file = self.current.open("r", encoding="utf-8")
        except FileNotFoundError:
            return
        with file:
            for line in file:
                name = line.strip()
                if name:
                    yield name

    @staticmethod
    def _records(names):
        first = True
        for name in names:
            if not isinstance(name, str) or not is_valid_username(name):
                raise RecoveryError("Invalid pending input; preserve the original inputs and review before retrying")
            yield (b"" if first else b"\n") + name.encode("utf-8")
            first = False

    def write(self, names):
        if not self._known:
            raise self._error("load and validate snapshots before writing")
        if iter(names) is names:
            raise TypeError("Pending snapshot input must be re-iterable")
        # Two streaming passes over a re-iterable dictionary view; no second
        # cohort-sized list/string or per-resolved-name disk operation.
        digest, count = hashlib.sha256(), 0
        for record in self._records(names):
            digest.update(record)
            count += 1
        target = digest.hexdigest()
        header = dict(count=count, digest=target, base=self._base)
        self._known = False
        with self.intent.open("wb") as file:
            file.write(_PREFIX + json.dumps(header, separators=(",", ":")).encode("ascii") + b"\n")
            for record in self._records(names):
                file.write(record)
            sync_file(file)
        _sync_directory(self.directory)
        self._promote(target)
        self._base, self._known = target, True
        self.has_checked_intent = True
