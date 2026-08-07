"""Which address Docker discovery reports for a container.

The provider used to report `http://<container-name>:<container-port>` always.
That name resolves only through Docker's embedded DNS, inside a container on the
same user-defined network — so for an agent installed on the host every
discovered container was unreachable. Worse, a published container had a working
host address available and threw it away, because the ports helper sorted
published ports first but returned the container-side number.

These tests pin the choice itself, which nothing covered before.
"""

from __future__ import annotations

import asyncio

import pytest

from hle_client.discovery.docker import DockerProvider, _candidates


def container(
    *,
    name="jellyfin",
    ports=None,
    networks=None,
    cid="abcdef1234567890",
    image="jellyfin/jellyfin",
    labels=None,
):
    return {
        "Id": cid,
        "Image": image,
        "Names": [f"/{name}"],
        "Ports": ports if ports is not None else [],
        "NetworkSettings": {"Networks": networks if networks is not None else {}},
        "Labels": labels or {},
    }


# Generous for a loopback connect (milliseconds locally), short enough that a
# container without usable loopback TCP skips rather than holding a CI runner.
SOCKET_TEST_BUDGET = 10.0

PUBLISHED = [{"Type": "tcp", "PrivatePort": 8096, "PublicPort": 32768, "IP": "0.0.0.0"}]
UNPUBLISHED = [{"Type": "tcp", "PrivatePort": 8096}]
BRIDGE = {"bridge": {"IPAddress": "172.17.0.4", "Aliases": []}}
USER_NET = {"media": {"IPAddress": "172.20.0.5", "Aliases": ["jellyfin", "media-server"]}}


class TestCandidateLadder:
    def test_host_agent_prefers_the_published_host_port(self):
        """The one address that works from the host, and Docker's own mapping."""
        cands = _candidates(container(ports=PUBLISHED, networks=USER_NET), in_container=False)
        assert (cands[0].host, cands[0].port) == ("127.0.0.1", 32768)
        assert cands[0].source == "published_port"
        assert cands[0].address == "http://127.0.0.1:32768"

    def test_container_agent_prefers_the_network_alias(self):
        cands = _candidates(container(ports=PUBLISHED, networks=USER_NET), in_container=True)
        assert cands[0].source == "container_dns"
        assert cands[0].host == "jellyfin"

    def test_a_specific_bind_address_is_used_rather_than_loopback(self):
        """Published to one interface only: 127.0.0.1 would not answer."""
        ports = [{"Type": "tcp", "PrivatePort": 8096, "PublicPort": 32768, "IP": "192.168.1.10"}]
        cands = _candidates(container(ports=ports), in_container=False)
        assert (cands[0].host, cands[0].port) == ("192.168.1.10", 32768)

    def test_unpublished_container_falls_back_to_its_bridge_ip(self):
        """The rung that exists because nothing is published to fall back to."""
        cands = _candidates(container(ports=UNPUBLISHED, networks=BRIDGE), in_container=False)
        assert (cands[0].host, cands[0].port) == ("172.17.0.4", 8096)
        assert cands[0].source == "container_ip"

    def test_every_rung_is_offered_as_a_fallback(self):
        cands = _candidates(container(ports=PUBLISHED, networks=USER_NET), in_container=False)
        assert [c.source for c in cands] == [
            "published_port",
            "container_ip",
            "container_dns",
            "container_dns",
        ]

    def test_all_aliases_are_candidates_not_just_the_first(self):
        """Dict order here is the daemon's; the reachable alias may not be first."""
        cands = _candidates(container(networks=USER_NET, ports=UNPUBLISHED), in_container=True)
        hosts = [c.host for c in cands]
        assert "jellyfin" in hosts and "media-server" in hosts

    def test_legacy_top_level_ip_is_used_when_networks_is_empty(self):
        c = container(ports=UNPUBLISHED)
        c["NetworkSettings"] = {"IPAddress": "172.17.0.9"}
        cands = _candidates(c, in_container=False)
        assert ("172.17.0.9", 8096) in [(x.host, x.port) for x in cands]

    def test_no_ports_yields_no_candidates(self):
        assert _candidates(container(), in_container=False) == []

    def test_udp_only_is_not_offered(self):
        c = container(ports=[{"Type": "udp", "PrivatePort": 51820, "PublicPort": 51820}])
        assert _candidates(c, in_container=False) == []


class TestScanPicksWhatAnswers:
    def _scan(self, containers, reachable, in_container=False):
        """Run scan() with the Engine API and the TCP probe both stubbed out."""
        probed: list[tuple[str, int]] = []

        async def fake_probe(host, port):
            probed.append((host, port))
            return (host, port) in reachable

        provider = DockerProvider(probe=fake_probe, in_container=in_container)
        return asyncio.run(provider.services_from(containers)), probed

    def test_reports_the_candidate_that_answers(self):
        """Host agent, container on a user-defined net: the alias never resolves."""
        services, probed = self._scan(
            [container(ports=PUBLISHED, networks=USER_NET)],
            reachable={("127.0.0.1", 32768)},
        )
        assert services[0].address == "http://127.0.0.1:32768"
        assert services[0].labels["hle.discovery.reachable"] == "true"
        assert services[0].labels["hle.discovery.address_source"] == "published_port"

    def test_skips_a_dead_rung_and_takes_the_next(self):
        """Published port bound but not answering; the bridge IP does."""
        services, probed = self._scan(
            [container(ports=PUBLISHED, networks=BRIDGE)],
            reachable={("172.17.0.4", 8096)},
        )
        assert services[0].address == "http://172.17.0.4:8096"
        assert ("127.0.0.1", 32768) in probed  # tried first, failed

    def test_unreachable_container_is_still_reported_and_labelled(self):
        """Dropping it would read as "discovery found nothing" — harder to debug."""
        services, _ = self._scan([container(ports=UNPUBLISHED, networks=USER_NET)], reachable=set())
        assert len(services) == 1
        assert services[0].labels["hle.discovery.reachable"] == "false"
        assert services[0].address.startswith("http://")

    def test_probing_stops_at_the_first_success(self):
        services, probed = self._scan(
            [container(ports=PUBLISHED, networks=USER_NET)],
            reachable={("127.0.0.1", 32768)},
        )
        assert probed == [("127.0.0.1", 32768)]

    def test_hle_containers_are_never_offered(self):
        services, _ = self._scan(
            [container(image="ghcr.io/hle-world/hle-docker:latest", ports=PUBLISHED)],
            reachable={("127.0.0.1", 32768)},
        )
        assert services == []

    def test_id_reflects_the_port_actually_reported(self):
        services, _ = self._scan([container(ports=PUBLISHED)], reachable={("127.0.0.1", 32768)})
        assert services[0].id == "abcdef123456:32768"

    def test_compose_metadata_survives_the_added_labels(self):
        services, _ = self._scan(
            [
                container(
                    ports=PUBLISHED,
                    labels={"com.docker.compose.project": "media", "build.irrelevant": "x"},
                )
            ],
            reachable={("127.0.0.1", 32768)},
        )
        assert services[0].namespace == "media"
        assert "build.irrelevant" not in services[0].labels


class TestProbeBudget:
    def test_a_hanging_probe_does_not_lose_the_scan(self, monkeypatch):
        """Overrunning the budget reports unverified addresses, not nothing."""
        monkeypatch.setattr("hle_client.discovery.docker.PROBE_BUDGET", 0.05)

        async def hangs(host, port):
            await asyncio.sleep(10)
            return True

        provider = DockerProvider(probe=hangs, in_container=False)
        cands = _candidates(container(ports=PUBLISHED), in_container=False)
        chosen = asyncio.run(provider._choose_addresses([cands]))
        assert chosen == [(cands[0], False)]

    def test_no_containers_needs_no_probing(self):
        async def explode(host, port):
            raise AssertionError("should not probe")

        provider = DockerProvider(probe=explode)
        assert asyncio.run(provider._choose_addresses([])) == []


class TestRealProbe:
    """Bounded, because an unbounded socket test wedged CI three times.

    These open a real loopback socket, which is the point — the probe's whole
    job is deciding whether something answers. But inside the CI container the
    3.12 job hung here indefinitely, holding a runner and starving the queue
    behind it for up to 95 minutes with no output. Every wait is now bounded, so
    an environment without usable loopback TCP skips with a reason instead of
    stopping the run.
    """

    def test_a_listening_socket_answers(self):
        async def go():
            server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            from hle_client.discovery.docker import _probe_tcp

            try:
                return await _probe_tcp("127.0.0.1", port)
            finally:
                server.close()
                await server.wait_closed()

        try:
            result = asyncio.run(asyncio.wait_for(go(), timeout=SOCKET_TEST_BUDGET))
        except TimeoutError:
            pytest.skip("no usable loopback TCP in this environment")
        assert result is True

    def test_nothing_listening_is_not_reachable(self):
        """A port that closed a moment ago: refused, not hung."""

        async def go():
            server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            server.close()
            await server.wait_closed()
            from hle_client.discovery.docker import _probe_tcp

            return await _probe_tcp("127.0.0.1", port)

        try:
            result = asyncio.run(asyncio.wait_for(go(), timeout=SOCKET_TEST_BUDGET))
        except TimeoutError:
            pytest.skip("no usable loopback TCP in this environment")
        assert result is False

    def test_a_name_that_does_not_resolve_is_not_reachable(self, monkeypatch):
        """The actual production bug: a container name, from the host.

        The resolver is stubbed rather than asked for a real name. `getaddrinfo`
        runs in a thread executor, so `wait_for` abandons the *wait* while the
        thread stays blocked in the syscall — and Python joins that thread at
        interpreter shutdown. Against a resolver that blackholes unknown names
        instead of answering NXDOMAIN, this test passed and then hung the whole
        run on the way out, which is exactly what it did on CI's 3.12.
        """
        import socket

        async def refuse_to_resolve(host, port, **kwargs):
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

        monkeypatch.setattr(asyncio, "open_connection", refuse_to_resolve)
        from hle_client.discovery.docker import _probe_tcp

        assert asyncio.run(_probe_tcp("jellyfin", 8096)) is False


@pytest.mark.parametrize(
    ("bind", "expected"),
    [
        ("", True),
        ("0.0.0.0", True),
        ("::", True),
        ("[::]", True),
        ("127.0.0.1", False),
        ("192.168.1.10", False),
        ("nonsense", False),  # unparseable: try it rather than assume loopback
    ],
)
def test_wildcard_bind_detection(bind, expected):
    from hle_client.discovery.docker import _is_wildcard_bind

    assert _is_wildcard_bind(bind) is expected


@pytest.mark.parametrize(
    ("env", "expected"),
    [("1", True), ("true", True), ("0", False), ("no", False)],
)
def test_hle_in_docker_overrides_inference(monkeypatch, env, expected):
    from hle_client.discovery.docker import running_in_container

    monkeypatch.setenv("HLE_IN_DOCKER", env)
    assert running_in_container() is expected
