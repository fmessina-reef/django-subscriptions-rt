from datetime import timedelta
from itertools import product
from operator import attrgetter

import pytest
from django.core.cache import caches
from freezegun import freeze_time
from subscriptions.exceptions import InconsistentQuotaCache, QuotaLimitExceeded
from subscriptions.functions import iter_subscriptions_involved, use_resource
from subscriptions.models import INFINITY, Quota, QuotaCache, QuotaChunk, Usage


def test_subscriptions_involved(five_subscriptions, user, plan, now, days):
    subscriptions_involved = iter_subscriptions_involved(user=user, at=now)
    assert sorted(subscriptions_involved, key=attrgetter('start')) == [
        five_subscriptions[1], five_subscriptions[3], five_subscriptions[0],
    ]


def test_subscriptions_involved_performance(five_subscriptions, django_assert_max_num_queries, user, now, plan):
    with django_assert_max_num_queries(2):
        list(iter_subscriptions_involved(user=user, at=now))


def test_cache_apply(resource, now, days):
    chunks = [
        QuotaChunk(resource=resource, start=now, end=now + days(1), remains=100),
        QuotaChunk(resource=resource, start=now + days(1), end=now + days(2), remains=100),
        QuotaChunk(resource=resource, start=now + days(2), end=now + days(3), remains=100),
    ]

    with pytest.raises(InconsistentQuotaCache):
        list(QuotaCache(
            datetime=now + days(2),
            chunks=chunks[::-1],
        ).apply(chunks))

    cache = QuotaCache(
        datetime=now + days(1),
        chunks=[
            QuotaChunk(resource=resource, start=now, end=now + days(1), remains=22),
            QuotaChunk(resource=resource, start=now + days(1), end=now + days(2), remains=33),
            QuotaChunk(resource=resource, start=now + days(2), end=now + days(3), remains=44),
        ],
    )

    assert list(cache.apply(chunks)) == cache.chunks


def test_remaining_chunks_performance(db, two_subscriptions, now, remaining_chunks, django_assert_max_num_queries, get_cache, days):
    cache_day, test_day = 8, 10

    with django_assert_max_num_queries(3):
        remaining_chunks(at=now + days(test_day))

    cache = get_cache(at=now + days(cache_day))
    with django_assert_max_num_queries(3):
        remaining_chunks(at=now + days(test_day), quota_cache=cache)


def test_usage_with_simple_quota(db, subscription, resource, remains, days):
    """
                     Subscription
    --------------[================]------------> time
    quota:    0   100            100   0

    -----------------|------|-------------------
    usage:           30     30
    """
    subscription.end = subscription.start + days(10)
    subscription.save(update_fields=['end'])

    Quota.objects.create(
        plan=subscription.plan,
        resource=resource,
        limit=50,  # but quantity == 2 -> real limit == 100
        recharge_period=INFINITY,
    )

    Usage.objects.bulk_create([
        Usage(user=subscription.user, resource=resource, amount=30, datetime=subscription.start + days(3)),
        Usage(user=subscription.user, resource=resource, amount=30, datetime=subscription.start + days(6)),
    ])

    assert remains(at=subscription.start) == 100
    assert remains(at=subscription.start + days(3)) == 70
    assert remains(at=subscription.start + days(6)) == 40
    assert remains(at=subscription.start + days(10)) == 0


def test_usage_with_recharging_quota(db, subscription, resource, remains, days):
    """
                         Subscription
    --------------[========================]------------> time

    quota 1:      [----------------]
             0    100           100  0

    quota 2:                   [-----------]
                          0    100       100  0

    -----------------|------|----|-------|-----------
    usage:           30     30   30      30
    """
    subscription.end = subscription.start + days(10)
    subscription.save(update_fields=['end'])

    Quota.objects.create(
        plan=subscription.plan,
        resource=resource,
        limit=50,  # but quantity == 2 -> real limit == 100
        recharge_period=days(5),
        burns_in=days(7),
    )

    Usage.objects.bulk_create([
        Usage(user=subscription.user, resource=resource, amount=amount, datetime=when)
        for amount, when in [
            (30, subscription.start + days(2)),
            (30, subscription.start + days(4)),
            (30, subscription.start + days(6)),
            (30, subscription.start + days(9)),
        ]
    ])

    assert remains(at=subscription.start) == 100
    assert remains(at=subscription.start + days(3)) == 70
    assert remains(at=subscription.start + days(4) + timedelta(hours=12)) == 40
    assert remains(at=subscription.start + days(5)) == 140
    assert remains(at=subscription.start + days(6)) == 110
    assert remains(at=subscription.start + days(7)) == 100
    assert remains(at=subscription.start + days(9)) == 70


def test_subtraction_priority(db, subscription, resource, remains, days):
    """
                         Subscription
    --------------[========================]------------> time

    quota 1:      [----------------]
             0    100           100  0

    quota 2:                   [---------------]
                          0    100           100  0

    -----------------------------|-------------------
    usage:                      150
    """
    subscription.end = subscription.start + days(10)
    subscription.save(update_fields=['end'])

    Quota.objects.create(
        plan=subscription.plan,
        resource=resource,
        limit=50,  # but quantity == 2 -> real limit == 100
        recharge_period=days(5),
        burns_in=days(7),
    )

    Usage.objects.create(
        user=subscription.user,
        resource=resource,
        amount=150,
        datetime=subscription.start + days(6),
    )

    assert remains(at=subscription.start + days(5)) == 200
    assert remains(at=subscription.start + days(6)) == 50
    assert remains(at=subscription.start + days(7)) == 50
    assert remains(at=subscription.start + days(10)) == 0


def test_multiple_subscriptions(db, two_subscriptions, user, resource, now, remains, days):

    assert remains(at=now - days(1)) == 0
    assert remains(at=now + days(0)) == 100
    assert remains(at=now + days(1)) == 50
    assert remains(at=now + days(2)) == 50
    assert remains(at=now + days(4)) == 150
    assert remains(at=now + days(5)) == 250
    assert remains(at=now + days(6)) == 50
    assert remains(at=now + days(7)) == 50
    assert remains(at=now + days(9)) == 150
    assert remains(at=now + days(10)) == 150
    assert remains(at=now + days(11)) == 100
    assert remains(at=now + days(12)) == 50
    assert remains(at=now + days(16)) == 0


def test_cache(db, two_subscriptions, now, remaining_chunks, get_cache, days):

    for cache_day, test_day in product(range(13), range(13)):
        if cache_day > test_day:
            continue

        assert remaining_chunks(at=now + days(test_day / 2), quota_cache=get_cache(at=now + days(cache_day / 2))) == remaining_chunks(at=now + days(test_day / 2))  # "middle" cases
        assert remaining_chunks(at=now + days(test_day), quota_cache=get_cache(at=now + days(cache_day))) == remaining_chunks(at=now + days(test_day))  # corner cases


def test_use_resource(db, user, subscription, quota, resource, remains, now, days):
    with freeze_time(now):
        assert remains() == 100
        with use_resource(user, resource, 10) as left:
            assert left == 90
            assert remains() == 90

        assert remains() == 90

    with freeze_time(now + days(1)):
        try:
            with use_resource(user, resource, 10) as left:
                assert remains() == left == 80
                raise ValueError()
        except ValueError:
            pass
        assert remains() == 90

    with freeze_time(now + days(2)):
        with pytest.raises(QuotaLimitExceeded):
            with use_resource(user, resource, 100):
                pass

    with freeze_time(now + days(2)):
        with use_resource(user, resource, 100, raises=False):
            pass


def test_cache_backend_correctness(cache_backend, db, user, two_subscriptions, remains, days, now, resource):
    cache = caches['subscriptions']

    assert cache.get(user.pk) is None

    assert remains(at=now - days(1)) == 0
    assert cache.get(user.pk) == QuotaCache(
        datetime=now - days(1),
        chunks=[],
    )

    assert remains(at=now) == 100
    assert cache.get(user.pk) == QuotaCache(
        datetime=now,
        chunks=[
            QuotaChunk(
                resource=resource,
                start=now,
                end=now + days(7),
                remains=100,
            ),
        ],
    )

    # corrupt cache
    cache.set(user.pk, QuotaCache(
        datetime=now,
        chunks=[
            QuotaChunk(
                resource=resource,
                start=now,
                end=now + days(4),
                remains=900,
            ),
        ],
    ))

    assert remains(at=now + days(1)) == 50
    assert cache.get(user.pk) == QuotaCache(
        datetime=now + days(1),
        chunks=[
            QuotaChunk(
                resource=resource,
                start=now,
                end=now + days(7),
                remains=50,
            ),
        ],
    )

    assert remains(at=now + days(6)) == 50
    assert cache.get(user.pk) == QuotaCache(
        datetime=now + days(6),
        chunks=[
            QuotaChunk(
                resource=resource,
                start=now,
                end=now + days(7),
                remains=0,
            ),
            QuotaChunk(
                resource=resource,
                start=now + days(4),
                end=now + days(4) + days(7),
                remains=50,
            ),
            QuotaChunk(
                resource=resource,
                start=now + days(5),
                end=now + days(10),
                remains=0,
            ),
        ],
    )
