"""Installation journal and atomic resource replacement for ExiZz Dual Subtitles."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

from w2_codec import SEPARATOR, decode_source, merge_pair
from w2_languages import LANGS, LOCALES, renderer_restriction, target_locale


STATE_DIR = "_ExiZz_DualSubtitles_State"
LEGACY_DIR = "_DualSubtitles_Backups"
SLOTS = set(LANGS.values())
SHA256 = re.compile(r"[0-9a-f]{64}")


class StateError(RuntimeError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def is_hash(value):
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def checked_path(path, is_directory=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    kind = stat.S_ISDIR if is_directory else stat.S_ISREG
    if not kind(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise StateError(f"Unexpected file type or link: {path}")
    return True


def read_file(path):
    if not checked_path(path):
        raise StateError(f"File missing: {path}")
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        data = handle.read()
        after = os.fstat(handle.fileno())
    current = path.lstat()
    def identity(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
    if identity(before) != identity(after) or identity(after) != identity(current):
        raise StateError(f"File changed while being read: {path.name}")
    return data


@dataclass(frozen=True)
class Pair:
    primary: str
    secondary: str
    target: str
    text_language: str

    @classmethod
    def plan(cls, primary, secondary):
        if primary not in LANGS or secondary not in LANGS or primary == secondary:
            raise StateError("Choose two different supported languages.")
        target = target_locale(LOCALES[primary], LOCALES[secondary])
        return cls(primary, secondary, target.filename, target.name)

    @classmethod
    def select(cls, primary, secondary):
        pair = cls.plan(primary, secondary)
        pair.require_installable()
        return pair

    def require_installable(self):
        if self != self.plan(self.primary, self.secondary):
            raise StateError("The target slot does not match the selected language pair.")
        reason = renderer_restriction(LOCALES[self.primary], LOCALES[self.secondary])
        if reason:
            raise StateError(reason)


class GameState:
    def __init__(self, game):
        self.cooked = (Path(game) / "CookedPC").resolve(strict=True)
        if not self.cooked.is_dir():
            raise StateError("CookedPC is not a directory.")
        self.directory = self.cooked / STATE_DIR
        self.objects = self.directory / "objects"
        self.journal = self.directory / "journal.json"

    @contextmanager
    def locked(self, create=True):
        if not checked_path(self.directory, is_directory=True):
            if not create:
                yield False
                return
            self.directory.mkdir(exist_ok=True)
            checked_path(self.directory, is_directory=True)
        lock = self.directory / "operation.lock"
        checked_path(lock)
        with lock.open("a+b") as handle:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise StateError("Another ExiZz Dual Subtitles operation is running for this game folder.") from error
            self.state = self.load()
            yield True
        # Closing the handle releases the OS lock, including on process termination.

    def load(self):
        if not checked_path(self.journal):
            return {"schema": 1, "slots": {}, "pending": None, "last": None}
        try:
            state = json.loads(read_file(self.journal))
            if type(state["schema"]) is not int or state["schema"] != 1:
                raise ValueError("unknown schema")
            slots = state["slots"]
            for name, entry in slots.items():
                if (name not in SLOTS or not is_hash(entry["original"])
                        or (entry["result"] is not None and not is_hash(entry["result"]))):
                    raise ValueError("invalid slot")
            pending = state["pending"]
            if pending is not None:
                if not pending["files"] or pending["kind"] not in ("install", "restore"):
                    raise ValueError("invalid pending operation")
                seen = set()
                for step in pending["files"]:
                    name = step["slot"]
                    if name not in SLOTS or name in seen or not all(
                        is_hash(step[key]) for key in ("original", "before", "after")
                    ):
                        raise ValueError("invalid pending step")
                    entry = slots.get(name)
                    if pending["kind"] == "restore":
                        if (not entry or not entry["result"] or entry["original"] != step["after"]
                                or step["before"] not in (entry["result"], entry["original"])):
                            raise ValueError("restore step does not match the active slot")
                    elif (entry and entry["result"] and
                          (entry["original"] != step["original"] or entry["result"] != step["before"])):
                        raise ValueError("install step does not match the active slot")
                    elif (not entry or not entry["result"]) and step["before"] != step["original"]:
                        raise ValueError("first install must start from its original")
                    seen.add(name)
            return state
        except (ValueError, KeyError, AttributeError, TypeError) as error:
            raise StateError("The transaction journal is invalid. Keep the state folder for manual recovery.") from error

    @contextmanager
    def staged(self, data):
        fd, name = tempfile.mkstemp(dir=self.directory, prefix="stage-")
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            yield path
        finally:
            path.unlink(missing_ok=True)

    def save(self, state):
        data = json.dumps(state, indent=2, ensure_ascii=False).encode("utf-8")
        with self.staged(data) as staged:
            os.replace(staged, self.journal)
        self.state = state

    def remember(self, data):
        key = digest(data)
        if not checked_path(self.objects, is_directory=True):
            self.objects.mkdir(exist_ok=True)
        path = self.objects / key
        if not checked_path(path):
            with self.staged(data) as staged:
                os.replace(staged, path)
        self.get_object(key)
        return key

    def get_object(self, key):
        checked_path(self.objects, is_directory=True)
        path = self.objects / key
        try:
            data = read_file(path)
        except StateError as error:
            raise StateError(f"Required backup or recovery snapshot is unavailable: {key}.") from error
        if digest(data) != key:
            raise StateError(f"Backup or recovery snapshot failed SHA-256 verification: {key}.")
        return data

    def live_digest(self, name):
        return digest(read_file(self.cooked / name))

    def check_live(self, name, expected):
        if self.live_digest(name) != expected:
            raise StateError(f"{name} changed outside this operation. The program will preserve it.")

    def replace(self, name, data, expected):
        with self.staged(data) as staged:
            # Recheck after staging and immediately before the atomic rename.
            self.check_live(name, expected)
            os.replace(staged, self.cooked / name)
        self.check_live(name, digest(data))

    def recover(self):
        pending = self.state["pending"]
        if pending is None:
            return False
        rollback = []
        for step in reversed(pending["files"]):
            name = step["slot"]
            before = self.get_object(step["before"])
            live = self.live_digest(name)
            if live == step["before"]:
                continue
            if live != step["after"]:
                raise StateError(f"Interrupted operation conflicts with an external change to {name}. "
                                 "Keep the journal and snapshots. No automatic recovery was applied.")
            rollback.append((name, before, live))
        for name, before, live in rollback:
            self.replace(name, before, live)
        self.save({**self.state, "pending": None})
        return True

    def commit(self, kind, files, slots, guard, sources=None):
        transaction = {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "files": files,
            "sources": sources or {},
        }
        for name, expected in guard.items():
            self.check_live(name, expected)
        self.save({**self.state, "pending": transaction})
        try:
            for step in files:
                if step["before"] != step["after"]:
                    self.replace(step["slot"], self.get_object(step["after"]), step["before"])
            expected = {**guard, **{step["slot"]: step["after"] for step in files}}
            for name, key in expected.items():
                self.check_live(name, key)
            self.save({**self.state, "slots": slots, "pending": None, "last": transaction})
        except Exception as error:
            try:
                self.recover()
            except Exception as recovery_error:
                raise StateError(f"{error}\nAutomatic rollback is pending: {recovery_error}") from error
            raise StateError(f"{error}\nThe transaction was rolled back.") from error

    def baseline(self, name, live, key):
        entry = self.state["slots"].get(name)
        if entry and entry["result"] is not None:
            if key != entry["result"]:
                raise StateError(f"{name} has an external change. Use Restore to reconcile an already "
                                 "restored original, or resolve the conflict before installing.")
            return self.get_object(entry["original"]), None
        if entry and key == entry["original"]:
            return self.get_object(key), None

        legacy_dir = self.cooked / LEGACY_DIR
        if checked_path(legacy_dir, is_directory=True):
            backup = legacy_dir / (name + ".original")
            if checked_path(backup) and digest(read_file(backup)) != key:
                raise StateError(f"Legacy backup for {name} differs from the live file. Verify the game "
                                 "files and archive this legacy backup outside CookedPC before retrying.")
        reason = "changed since verification" if entry else "first use"
        return live, reason

    def install(self, pair, gibbed, approve):
        if checked_path(self.directory / "font-prototype.json"):
            raise StateError("Restore the active P2 font test with the P2 command first.")
        pair.require_installable()
        return self._install(pair, gibbed, approve)

    def _install(self, pair, gibbed, approve):
        if self.state["pending"] is not None:
            raise StateError("Recover the interrupted operation before installing.")
        names = LANGS[pair.primary], LANGS[pair.secondary]
        original, guard, new = {}, {}, {}
        for name in names:
            live = read_file(self.cooked / name)
            guard[name] = digest(live)
            original[name], reason = self.baseline(name, live, guard[name])
            if reason:
                new[name] = reason

        with tempfile.TemporaryDirectory(prefix="w2dualsubs-prepare-") as directory:
            work = Path(directory)
            decoded = [decode_source(original[name], Path(name).stem, gibbed, work) for name in names]
            for name, source, other in zip(names, decoded, reversed(decoded)):
                if name not in new or not source.marked_ids:
                    continue
                for index in source.marked_ids:
                    donor = other.texts.get(index)
                    current = source.texts.get(index)
                    if donor and current and (
                        current.startswith(donor + SEPARATOR) or current.endswith(SEPARATOR + donor)
                    ):
                        raise StateError(f"{name} contains the dual separator and the other language's text "
                                         f"at ID {index}. Verify clean game files before using it.")
                new[name] += f"; found the dual separator in {len(source.marked_ids):,} records"
            base = decoded[names.index(pair.target)]
            result = merge_pair(*decoded, base, Path(pair.target).stem, gibbed, work)

        if new and not approve(new):
            return None
        originals = {name: self.remember(data) for name, data in original.items()}
        for name, key in guard.items():
            self.check_live(name, key)
        live_target = read_file(self.cooked / pair.target)
        if digest(live_target) != guard[pair.target]:
            raise StateError(f"{pair.target} changed while preparing the update.")
        step = {"slot": pair.target, "original": originals[pair.target],
                "before": self.remember(live_target), "after": self.remember(result.data)}
        slots = self.state["slots"].copy()
        for name, key in originals.items():
            if name == pair.target:
                slots[name] = {"original": key, "result": step["after"],
                               "pair": [pair.primary, pair.secondary]}
            elif name in new:
                slots[name] = {"original": key, "result": None}
        self.commit("install", [step], slots, guard, originals)
        return result

    def restore(self, approve):
        if checked_path(self.directory / "font-prototype.json"):
            raise StateError("Restore the active P2 font test with the P2 command first.")
        if self.state["pending"] is not None:
            raise StateError("Recover the interrupted operation before restoring.")
        files, guard = [], {}
        slots = self.state["slots"].copy()
        for name, entry in slots.items():
            installed, original = entry["result"], entry["original"]
            if installed is None:
                continue
            self.get_object(original)
            live = self.live_digest(name)
            if live not in (installed, original):
                raise StateError(f"{name} has been changed outside this program. Restore preserved it.")
            guard[name] = live
            if live != original:
                self.get_object(installed)
            files.append({"slot": name, "original": original, "before": live, "after": original})
        if not guard or not approve(tuple(guard)):
            return ()
        for name in guard:
            slots[name] = {"original": slots[name]["original"], "result": None}
        self.commit("restore", files, slots, guard)
        return tuple(guard)
