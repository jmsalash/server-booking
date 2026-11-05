"""Is this the same physical machine as a server we already have?

Two servers are the same machine when any of their normalised keys (hostname / Grafana
instance, port stripped, lower-cased) are equal, or when the IP addresses those keys resolve to
intersect. DNS is best effort: one bounded round of lookups per request; a name that fails or
times out is only compared by name, so a save is never blocked on DNS.
"""
import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable

from app.integrations.grafana import strip_port

Resolver = Callable[[str], Awaitable[set[str]]]
DNS_TIMEOUT = 2.0


def _ip(value: str) -> str | None:
    """Canonical text of an IP literal (brackets and zone dropped), else None."""
    try:
        return str(ipaddress.ip_address(value.strip("[]").split("%", 1)[0]))
    except ValueError:
        return None


def machine_keys(*values: str | None) -> set[str]:
    keys = set()
    for v in values:
        if v and (k := strip_port(v).lower()):
            keys.add(_ip(k) or k)
    return keys


async def resolve(name: str) -> set[str]:
    infos = await asyncio.to_thread(socket.getaddrinfo, name, None)
    return {ip for info in infos if (ip := _ip(str(info[4][0])))}


class MachineIndex:
    def __init__(self, resolver: Resolver | None = None, timeout: float = DNS_TIMEOUT):
        self._resolver = resolver or resolve
        self._timeout = timeout
        self._addrs: dict[str, set[str]] = {}  # resolved names (request-scoped cache)
        self._machines: list[tuple[int | None, str, set[str]]] = []

    async def load(self, machines: Iterable[tuple[int | None, str, set[str]]],
                   extra: Iterable[set[str]] = ()) -> None:
        """Register existing machines and resolve every key (theirs and `extra`) at once."""
        self._machines.extend(machines)
        names = {k for _, _, keys in self._machines for k in keys}
        names |= {k for keys in extra for k in keys}
        todo = {n for n in names if _ip(n) is None and n not in self._addrs}
        if not todo:
            return
        tasks = {asyncio.ensure_future(self._resolver(n)): n for n in todo}
        done, pending = await asyncio.wait(tasks, timeout=self._timeout)
        for t in pending:
            t.cancel()
        for t in done:
            if not t.cancelled() and t.exception() is None:
                self._addrs[tasks[t]] = {a for x in t.result() if (a := _ip(x))}

    def addresses(self, keys: set[str]) -> set[str]:
        out = set()
        for k in keys:
            ip = _ip(k)
            out |= {ip} if ip else self._addrs.get(k, set())
        return out

    def find(self, keys: set[str], exclude_id: int | None = None) -> str | None:
        """Name of an existing machine with a common key or address, else None."""
        addrs = self.addresses(keys)
        for mid, name, other in self._machines:
            if exclude_id is not None and mid == exclude_id:
                continue
            if keys & other or addrs & self.addresses(other):
                return name
        return None

    def add(self, name: str, keys: set[str]) -> None:
        """Register a machine accepted earlier in the same batch."""
        self._machines.append((None, name, keys))
