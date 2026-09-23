"""The sampled rollout decision is a pure function of the trial id, the attempt
and two settings, so a trial always gets the same answer."""

from __future__ import annotations

from oddish.workers.launcher.route import BUCKETS, bucket_for, monitor_route


def test_buckets_are_stable_and_spread():
    ids = [f"archil40-20260915-{n:02d}-{n * 7919:08x}-{n % 6}" for n in range(2000)]
    buckets = [bucket_for(i) for i in ids]
    assert buckets == [bucket_for(i) for i in ids]  # deterministic
    assert set(buckets) == set(range(BUCKETS))  # every bucket used
    share = sum(1 for b in buckets if b < 10) / len(buckets)
    assert 0.07 < share < 0.13  # 10 percent, give or take


def test_percent_100_takes_every_attempt_and_percent_0_none():
    for attempt in (1, 2, 3, 6):
        r = monitor_route("t-1", attempt, percent=100, fallback_after=2)
        assert r.monitor and r.name == "monitor" and r.reason == "percent=100"
        r = monitor_route("t-1", attempt, percent=0, fallback_after=2)
        assert not r.monitor and r.name == "worker" and r.reason == "percent=0"


def test_a_sampled_trial_falls_back_to_the_worker_after_the_configured_attempts():
    sampled = next(i for i in (f"t-{n}" for n in range(1000)) if bucket_for(i) < 50)
    unsampled = next(i for i in (f"t-{n}" for n in range(1000)) if bucket_for(i) >= 50)
    assert monitor_route(sampled, 1, percent=50, fallback_after=2).monitor
    assert monitor_route(sampled, 2, percent=50, fallback_after=2).monitor
    third = monitor_route(sampled, 3, percent=50, fallback_after=2)
    assert not third.monitor and third.reason == "fallback attempt=3>2"
    never = monitor_route(unsampled, 1, percent=50, fallback_after=2)
    assert not never.monitor and never.reason.startswith("not-sampled bucket=")


def test_one_percent_is_bucket_zero_only():
    hits = [
        i
        for i in (f"t-{n}" for n in range(5000))
        if monitor_route(i, 1, percent=1, fallback_after=2).monitor
    ]
    assert hits and all(bucket_for(i) == 0 for i in hits)
    assert 0.004 < len(hits) / 5000 < 0.02
