"""Login throttling: lock a username/IP pair after repeated failures."""

import hashlib

from django.conf import settings
from django.core.cache import cache


def _key(username: str, ip: str | None) -> str:
    raw = f"{(username or '').lower()}|{ip or ''}"
    return "lg:loginfail:" + hashlib.sha256(raw.encode()).hexdigest()


def _user_key(username: str) -> str:
    return "lg:loginfail-user:" + hashlib.sha256((username or "").lower().encode()).hexdigest()


def is_locked(username: str, ip: str | None) -> bool:
    limit = settings.LEDGERGUARD["LOGIN_MAX_FAILURES"]
    # Per-username limit is looser so that distributed guessing is also slowed.
    return cache.get(_key(username, ip), 0) >= limit or cache.get(_user_key(username), 0) >= limit * 4


def register_failure(username: str, ip: str | None) -> int:
    timeout = settings.LEDGERGUARD["LOGIN_LOCKOUT_SECONDS"]
    for key in (_key(username, ip), _user_key(username)):
        cache.add(key, 0, timeout)
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout)
    return cache.get(_key(username, ip), 0)


def reset(username: str, ip: str | None):
    cache.delete_many([_key(username, ip), _user_key(username)])
