import asyncio

from app.services.machine_identity import MachineIndex, machine_keys


def resolver(table: dict[str, set[str]], calls: list | None = None):
    async def resolve(name: str) -> set[str]:
        if calls is not None:
            calls.append(name)
        if name not in table:
            raise OSError(f"cannot resolve {name}")
        return table[name]
    return resolve


def test_machine_keys_normalise():
    assert machine_keys("GPU-01.lab:9100", "", None, "gpu-01.lab") == {"gpu-01.lab"}
    assert machine_keys("[::1]:9100") == {"::1"}
    assert machine_keys("10.0.0.5:9100") == {"10.0.0.5"}


async def test_same_hostname_different_case_and_port():
    idx = MachineIndex(resolver({}))
    await idx.load([(1, "gpu-01", machine_keys("gpu-01.lab"))], [machine_keys("GPU-01.LAB:9100")])
    assert idx.find(machine_keys("GPU-01.LAB:9100")) == "gpu-01"


async def test_ip_vs_hostname_resolving_to_it():
    idx = MachineIndex(resolver({"gpu-01.lab": {"10.0.0.5"}}))
    await idx.load([(1, "gpu-01", machine_keys("gpu-01.lab"))], [machine_keys("10.0.0.5:9100")])
    assert idx.find(machine_keys("10.0.0.5:9100")) == "gpu-01"
    # and the other way round
    idx = MachineIndex(resolver({"gpu-01.lab": {"10.0.0.5"}}))
    await idx.load([(1, "by-ip", machine_keys("10.0.0.5"))], [machine_keys("gpu-01.lab")])
    assert idx.find(machine_keys("gpu-01.lab")) == "by-ip"


async def test_two_hostnames_same_ip():
    idx = MachineIndex(resolver({"a.lab": {"10.0.0.5", "fe80::1"}, "alias.lab": {"10.0.0.5"}}))
    await idx.load([(1, "a", machine_keys("a.lab"))], [machine_keys("alias.lab")])
    assert idx.find(machine_keys("alias.lab")) == "a"


async def test_resolver_failure_falls_back_to_name_check():
    idx = MachineIndex(resolver({"alias.lab": {"10.0.0.5"}}))  # a.lab does not resolve
    await idx.load([(1, "a", machine_keys("a.lab"))], [machine_keys("alias.lab"), {"a.lab"}])
    assert idx.find(machine_keys("alias.lab")) is None
    assert idx.find(machine_keys("A.lab:22")) == "a"


async def test_resolver_timeout_is_bounded_and_skipped():
    async def slow(name):
        if name == "slow.lab":
            await asyncio.sleep(10)
        return {"10.0.0.9"}

    idx = MachineIndex(slow, timeout=0.05)
    await asyncio.wait_for(idx.load([(1, "s", {"slow.lab"})], [{"fast.lab"}]), 1)
    assert idx.find({"fast.lab"}) is None  # slow.lab's addresses are unknown, not a match


async def test_edit_excludes_itself():
    idx = MachineIndex(resolver({"a.lab": {"10.0.0.5"}}))
    await idx.load([(1, "a", machine_keys("a.lab")), (2, "b", machine_keys("b.lab"))])
    assert idx.find(machine_keys("a.lab"), exclude_id=1) is None
    assert idx.find(machine_keys("a.lab"), exclude_id=2) == "a"


async def test_resolution_cached_and_ip_literals_not_resolved():
    calls = []
    idx = MachineIndex(resolver({"a.lab": {"10.0.0.5"}}, calls))
    await idx.load([(1, "a", {"a.lab"}), (2, "b", {"10.0.0.7"})], [{"a.lab"}, {"10.0.0.5"}])
    assert calls == ["a.lab"]
    assert idx.find({"10.0.0.5"}) == "a"


async def test_add_detects_duplicates_within_a_batch():
    idx = MachineIndex(resolver({"x.lab": {"10.0.0.1"}}))
    await idx.load([], [{"x.lab"}, {"10.0.0.1"}])
    assert idx.find({"x.lab"}) is None
    idx.add("x", {"x.lab"})
    assert idx.find({"10.0.0.1"}) == "x"
