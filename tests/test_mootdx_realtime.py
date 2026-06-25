"""Tests for MootdxRealtimeSource (no TCP connection needed for unit tests)."""

from data.sources.mootdx_realtime import MootdxRealtimeSource


def test_bj_filter():
    source = MootdxRealtimeSource(batch_size=100, min_interval=0)
    assert source._BJ_PREFIXES == ("8", "920")


def test_ip_pool_nonempty():
    source = MootdxRealtimeSource()
    assert len(source.ip_pool) > 0
    ip, port = source._pick_server()
    assert isinstance(ip, str) and len(ip) > 7
    assert isinstance(port, int) and port > 0


def test_rate_limit():
    import time
    source = MootdxRealtimeSource(min_interval=0.5)
    t0 = time.time()
    source._rate_limit()
    t1 = time.time()
    source._rate_limit()
    t2 = time.time()
    assert (t2 - t1) >= 0.3
