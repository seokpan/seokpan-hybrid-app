from seokpan.clock import SystemClock


def test_system_clock_returns_epoch_milliseconds(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("seokpan.clock.time.time_ns", lambda: 1_725_000_123_456_789_000)

    assert SystemClock().now_ms == 1_725_000_123_456
