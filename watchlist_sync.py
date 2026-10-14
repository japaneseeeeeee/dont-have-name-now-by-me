"""Shared helpers for the GitHub-canonical watchlist and the Mac fallback copy."""

import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone


DEFAULT_BACKUP_LIMIT = 10


def _copy_default(default):
    return default.copy() if isinstance(default, dict) else list(default)


def read_json_locked(path, default):
    """Read JSON while sharing the same lock used by the monitor and Discord bot."""
    with open(path + ".lock", "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_SH)
        try:
            if not os.path.exists(path):
                return _copy_default(default)
            with open(path, "r", encoding="utf-8") as stream:
                return json.load(stream)
        except (OSError, ValueError, json.JSONDecodeError):
            return _copy_default(default)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def watchlist_digest(data):
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_json_atomic_unlocked(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    os.replace(tmp_path, path)


def _backup_name(data, now=None):
    current = now or datetime.now(timezone.utc)
    stamp = current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"watchlist-{stamp}-{watchlist_digest(data)[:8]}.json"


def _prune_backups(backup_dir, limit):
    names = sorted(
        name for name in os.listdir(backup_dir)
        if name.startswith("watchlist-") and name.endswith(".json")
    )
    for name in names[:-max(1, int(limit))]:
        try:
            os.unlink(os.path.join(backup_dir, name))
        except OSError:
            pass


def replace_local_watchlist(path, data, backup_dir, *, limit=DEFAULT_BACKUP_LIMIT, now=None):
    """Atomically replace the Mac copy, saving the previous valid version first."""
    if not isinstance(data, dict):
        raise ValueError("watchlist must be an object")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    os.makedirs(backup_dir, exist_ok=True)
    with open(path + ".lock", "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            previous = None
            try:
                with open(path, "r", encoding="utf-8") as stream:
                    candidate = json.load(stream)
                if isinstance(candidate, dict):
                    previous = candidate
            except (OSError, ValueError, json.JSONDecodeError):
                previous = None
            if previous == data:
                return {"changed": False, "backup": None}

            backup_path = None
            if previous is not None:
                backup_path = os.path.join(backup_dir, _backup_name(previous, now))
                if not os.path.exists(backup_path):
                    _write_json_atomic_unlocked(backup_path, previous)
                _prune_backups(backup_dir, limit)

            _write_json_atomic_unlocked(path, data)
            return {"changed": True, "backup": backup_path}
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def backup_count(backup_dir):
    try:
        return sum(
            1 for name in os.listdir(backup_dir)
            if name.startswith("watchlist-") and name.endswith(".json")
        )
    except OSError:
        return 0


def save_sync_status(path, status):
    payload = dict(status)
    payload.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
    with open(path + ".lock", "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            if not payload.get("last_success_at"):
                try:
                    with open(path, "r", encoding="utf-8") as stream:
                        previous = json.load(stream)
                    payload["last_success_at"] = str(previous.get("last_success_at") or "")
                except (OSError, ValueError, json.JSONDecodeError, AttributeError):
                    pass
            _write_json_atomic_unlocked(path, payload)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
    return payload


def build_sync_status(remote, local, *, source, error="", repaired=False, now=None):
    remote_ok = isinstance(remote, dict)
    local_ok = isinstance(local, dict)
    remote_hash = watchlist_digest(remote) if remote_ok else ""
    local_hash = watchlist_digest(local) if local_ok else ""
    current = now or datetime.now(timezone.utc)
    return {
        "updated_at": current.astimezone(timezone.utc).isoformat(),
        "last_success_at": current.astimezone(timezone.utc).isoformat() if remote_ok else "",
        "source": source,
        "remote_count": len(remote) if remote_ok else None,
        "local_count": len(local) if local_ok else None,
        "remote_hash": remote_hash,
        "local_hash": local_hash,
        "in_sync": bool(remote_ok and local_ok and remote_hash == local_hash),
        "repaired": bool(repaired),
        "error": str(error or "")[:500],
    }
