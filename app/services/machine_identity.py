"""Is this the same physical machine as a server we already have?

Two servers are the same machine when any of their normalised keys (hostname / Grafana
instance, port stripped, lower-cased) are equal, or when the IP addresses those keys resolve to
intersect. DNS is best effort: one bounded round of lookups per request; a name that fails or
times out is only compared by name, so a save is never blocked on DNS. Addresses that do not
identify a machine (loopback such as Debian's 127.0.1.1, link-local, multicast, ...) are ignored.
"""
import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from concurrent.futures import ThreadPoolExecutor

from app.integrations.grafana import strip_port

Resolver = Callable[[str], Awaitable[set[str]]]
DNS_TIMEOUT = 2.0
# Own small pool, so slow resolvers cannot starve other asyncio.to_thread users.
_DNS_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="dns")


def _ip(value: str) -> str | None:
    """Canonical text of an IP literal (brackets/zone dropped, IPv4-mapped unwrapped), else None."""
    try:
        ip = ipaddress.ip_address(value.strip("[]").split("%", 1)[0])
    except ValueError:
        return None
    return str(getattr(ip, "ipv4_mapped", None) or ip)


def _identifying(value: str) -> str | None:
    """Canonical address if it can identify one machine, else None."""
    text = _ip(value)
    if text is None:
        return None
    ip = ipaddress.ip_address(text)
    if (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast
            or ip.is_reserved):
        return None
    return text


def machine_keys(*values: str | None) -> set[str]:
    keys = set()
    for v in values:
        if v and (k := strip_port(v).lower()):
            keys.add(_ip(k) or k)
    return keys


async def resolve(name: str) -> set[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.run_in_executor(_DNS_POOL, socket.getaddrinfo, name, None)
    return {ip for info in infos if (ip := _identifying(str(info[4][0])))}


class MachineIndex:
    def __init__(self, resolver: Resolver | None = None, timeout: float = DNS_TIMEOUT):
        self._resolver = resolver or resolve
        self._timeout = timeout
        self._addrs: dict[str, set[str]] = {}  # resolved names (request-scoped cache)
        self._machines: list[tuple[int | None, str, set[str]]] = []

    async def load(self, machines: Iterable[tuple[int | None, str, set[str]]],
                   extra: Iterable[set[str]] = (), resolve: bool = True) -> None:
        """Register existing machines and resolve every key (theirs and `extra`) at once."""
        self._machines.extend(machines)
        if not resolve:
            return
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
                self._addrs[tasks[t]] = {a for x in t.result() if (a := _identifying(x))}

    def addresses(self, keys: set[str]) -> set[str]:
        out = set()
        for k in keys:
            if _ip(k) is not None:
                out |= {ip} if (ip := _identifying(k)) else set()
            else:
                out |= self._addrs.get(k, set())
        return out

    def find(self, keys: set[str], exclude_id: int | None = None,
             use_addresses: bool = True) -> str | None:
        """Name of an existing machine with a common key (or address), else None."""
        addrs = self.addresses(keys) if use_addresses else set()
        for mid, name, other in self._machines:
            if exclude_id is not None and mid == exclude_id:
                continue
            if keys & other or (addrs and addrs & self.addresses(other)):
                return name
        return None

    def add(self, name: str, keys: set[str]) -> None:
        """Register a machine accepted earlier in the same batch."""
        self._machines.append((None, name, keys))
