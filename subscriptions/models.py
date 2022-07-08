from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import count, islice, zip_longest
from logging import getLogger
from operator import attrgetter
from typing import TYPE_CHECKING, Iterable, Iterator, List, Optional

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Index, QuerySet, UniqueConstraint
from django.urls import reverse
from django.utils.timezone import now

from .exceptions import InconsistentQuotaCache, PaymentError, ProlongationImpossible, ProviderNotFound, QuotaLimitExceeded
from .fields import MoneyField, RelativeDurationField
from .utils import merge_iter

log = getLogger(__name__)

if TYPE_CHECKING:
    from .providers import Provider

INFINITY = relativedelta(days=365 * 1000)


class Resource(models.Model):
    codename = models.CharField(max_length=255)
    units = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=['codename'], name='unique_resource'),
        ]

    def __str__(self) -> str:
        return self.codename


class Plan(models.Model):
    codename = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    charge_amount = MoneyField(blank=True, null=True)
    charge_period = RelativeDurationField(blank=True, help_text='leave blank for one-time charge')
    max_duration = RelativeDurationField(blank=True, help_text='leave blank to make it an infinite subscription')
    is_enabled = models.BooleanField(default=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=['codename'], name='unique_plan_codename'),
        ]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse('plan', kwargs={'plan_id': self.id})

    def save(self, *args, **kwargs):
        self.charge_period = self.charge_period or INFINITY
        self.max_duration = self.max_duration or INFINITY
        return super().save(*args, **kwargs)

    def is_recurring(self) -> bool:
        return self.charge_period != INFINITY


@dataclass
class QuotaChunk:
    resource: Resource
    start: datetime
    end: datetime
    remains: int

    def __str__(self) -> str:
        return f'{self.remains} {self.resource} {self.start} - {self.end}'

    def includes(self, date: datetime) -> bool:
        return self.start <= date < self.end

    def same_lifetime(self, other: 'QuotaChunk') -> bool:
        return self.start == other.start and self.end == other.end


@dataclass
class QuotaCache:
    datetime: datetime
    chunks: List[QuotaChunk]

    def apply(self, chunks: Iterable[QuotaChunk]) -> Iterator[QuotaChunk]:
        cached_chunks = iter(self.chunks)

        # match chunks and cached_chunks one-by-one
        check_cached_pair = True
        for i, (chunk, cached_chunk) in enumerate(zip_longest(chunks, cached_chunks, fillvalue=None)):
            if not chunk and cached_chunk:
                raise InconsistentQuotaCache(f'Non-paired cached chunk detected at position {i}: {cached_chunk}')

            elif chunk and cached_chunk:
                if not chunk.same_lifetime(cached_chunk):
                    raise InconsistentQuotaCache(f'Non-matched cached chunk detected at position {i}: {chunk=}, {cached_chunk=}')

                yield cached_chunk

            elif chunk and not cached_chunk:
                if check_cached_pair:
                    if chunk.includes(self.datetime):
                        raise InconsistentQuotaCache(f'No cached chunk for {chunk}')
                    check_cached_pair = False

                yield chunk


class SubscriptionQuerySet(models.QuerySet):
    def active(self, at: Optional[datetime] = None) -> QuerySet:
        at = at or now()
        return self.filter(start__lte=at, end__gt=at)

    def expiring(self, within: datetime, from_: Optional[datetime] = None) -> QuerySet:
        from_ = from_ or now()
        return self.filter(end__gte=from_, end__lte=from_ + within)

    def recurring(self, value: bool = True) -> QuerySet:
        subscriptions = self.select_related('plan')
        return subscriptions.exclude(plan__charge_period=INFINITY) if value else subscriptions.filter(plan__charge_period=INFINITY)


class Subscription(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='subscriptions')
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name='subscriptions')
    quantity = models.PositiveIntegerField(default=1)
    start = models.DateTimeField(blank=True)
    end = models.DateTimeField(blank=True)

    objects = SubscriptionQuerySet.as_manager()

    def __str__(self) -> str:
        return f'{self.user} @ {self.plan}, {self.start} - {self.end}'

    @property
    def max_end(self) -> datetime:
        return self.start + self.plan.max_duration

    def save(self, *args, **kwargs):
        self.start = self.start or now()
        self.end = self.end or min(self.start + self.plan.charge_period, self.max_end)
        return super().save(*args, **kwargs)

    def stop(self):
        self.end = now()
        self.save(update_fields=['end'])

    @classmethod
    def get_expiring(cls, within: timedelta) -> QuerySet:
        return cls.objects.active().expiring(within)

    def prolong(self):
        next_charge_dates = islice(self.iter_charge_dates(since=self.end), 2)
        if (first_charge_date := next(next_charge_dates)) and self.end == first_charge_date:
            try:
                end = next(next_charge_dates)
            except StopIteration as exc:
                raise ProlongationImpossible('No next charge date') from exc
        else:
            end = first_charge_date

        if end > (max_end := self.max_end):
            if self.end == max_end:
                raise ProlongationImpossible('Current subscription end is already the maximum end')

            end = max_end

        self.end = end

    def iter_quota_chunks(
        self,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        sort_by: callable = attrgetter('start'),
    ) -> Iterator[QuotaChunk]:

        quotas = self.plan.quotas.all()
        yield from merge_iter(
            *(self._iter_single_quota_chunks(quota=quota, since=since, until=until) for quota in quotas),
            key=sort_by,
        )

    def _iter_single_quota_chunks(self, quota: 'Quota', since: Optional[datetime] = None, until: Optional[datetime] = None):

        epsilon = timedelta(milliseconds=1)  # we use epsilon to exclude chunks which start right at `since - quota.burns_in`
        min_start_time = max(since - quota.burns_in + epsilon, self.start) if since else self.start  # quota chunks starting after this are OK
        until = min(until, self.end) if until else self.end

        for i in count(start=0):
            start = self.start + i * quota.recharge_period
            if start < min_start_time:
                continue

            if start > until:
                return

            yield QuotaChunk(
                resource=quota.resource,
                start=start,
                end=min(start + quota.burns_in, self.end),
                remains=quota.limit * self.quantity,
            )

    def iter_charge_dates(self, since: Optional[datetime] = None) -> Iterator[datetime]:
        """ Including first charge (i.e. charge to create subscription) """
        charge_period = self.plan.charge_period
        since = since or self.start

        for i in count(start=0):
            charge_date = self.start + charge_period * i

            if charge_date < since:
                continue

            yield charge_date
            if charge_period == INFINITY:
                return

    def charge_offline(self):
        from .providers import get_provider

        last_payment = SubscriptionPayment.get_last_successful(self.user)
        if not last_payment:
            raise PaymentError('There is no previous successful payment to take credentials from')

        provider_codename = last_payment.provider_codename
        try:
            provider = get_provider(provider_codename)
        except ProviderNotFound as exc:
            raise PaymentError(f'Could not retrieve provider "{provider_codename}"') from exc

        provider.charge_offline(user=self.user, plan=self.plan, subscription=self)

    def send_successful_charge_email(self):
        raise NotImplementedError()  # TODO

    def send_failed_charge_email(self):
        raise NotImplementedError()  # TODO


class Quota(models.Model):
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name='quotas')
    resource = models.ForeignKey(Resource, on_delete=models.CASCADE, related_name='quotas')
    limit = models.PositiveIntegerField()
    recharge_period = RelativeDurationField(blank=True, help_text='leave blank for recharging only after each subscription prolongation (charge)')
    burns_in = RelativeDurationField(blank=True, help_text='leave blank to burn each recharge period')

    class Meta:
        constraints = [
            UniqueConstraint(fields=['plan', 'resource'], name='unique_quota'),
        ]

    def __str__(self) -> str:
        return f'{self.resource} {self.limit:,}{self.resource.units}/{self.recharge_period}, burns in {self.burns_in}'

    def save(self, *args, **kwargs):
        self.recharge_period = self.recharge_period or self.plan.charge_period
        self.burns_in = self.burns_in or self.recharge_period
        return super().save(*args, **kwargs)


class Usage(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='usages')
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name='usages')
    amount = models.PositiveIntegerField(default=1)
    datetime = models.DateTimeField(blank=True)

    class Meta:
        indexes = [
            Index(fields=['user', 'resource']),
        ]

    def __str__(self) -> str:
        return f'{self.amount:,}{self.resource.units} {self.resource} at {self.datetime}'

    def save(self, *args, **kwargs):
        from .functions import get_remaining_amount

        self.datetime = self.datetime or now()

        remains = get_remaining_amount(user=self.user, at=self.datetime).get(self.resource, 0)
        if remains < self.amount:
            raise QuotaLimitExceeded(f'Tried to use {self.amount} {self.resource}(s) while only {remains} is allowed')

        return super().save(*args, **kwargs)


class AbstractTransaction(models.Model):

    class Status(models.IntegerChoices):
        PENDING = 0
        PREAUTH = 1
        COMPLETED = 2
        CANCELED = 3
        ERROR = 4

    provider_codename = models.CharField(max_length=255)
    provider_transaction_id = models.CharField(max_length=255, blank=True, null=True)
    status = models.PositiveSmallIntegerField(choices=Status.choices, default=Status.PENDING)
    amount = MoneyField()
    # source = models.ForeignKey(MoneyStorage, on_delete=models.PROTECT, related_name='transactions_out')
    # destination = models.ForeignKey(MoneyStorage, on_delete=models.PROTECT, related_name='transactions_in')
    metadata = models.JSONField(blank=True, default=dict, encoder=DjangoJSONEncoder)
    created = models.DateTimeField(blank=True, editable=False)
    updated = models.DateTimeField(blank=True, editable=False)

    class Meta:
        abstract = True
        indexes = [
            Index(fields=('provider_codename', 'provider_transaction_id')),
        ]

    def save(self, *args, **kwargs):
        now_ = now()
        self.created = self.created or now_
        self.updated = now_
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f'{self.get_status_display()} {self.amount} via {self.provider_codename}'

    @property
    def provider(self) -> Provider:
        from .providers import get_provider
        return get_provider(self.provider_codename)


class SubscriptionPayment(AbstractTransaction):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='payments')
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name='payments')
    subscription = models.ForeignKey(Subscription, on_delete=models.PROTECT, blank=True, null=True, related_name='payments')
    subscription_start = models.DateTimeField(blank=True, null=True)
    subscription_end = models.DateTimeField(blank=True, null=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._initial_status = self.status

    def save(self, *args, **kwargs):
        if self.status == self.Status.COMPLETED and any((
            not self.id,
            self._initial_status != self.Status.COMPLETED,
        )):
            if (subscription := self.subscription):
                self.subscription_start = subscription.end
                subscription.prolong()
                self.subscription_end = subscription.end
                subscription.save()
            else:
                self.subscription = Subscription.objects.create(
                    user=self.user,
                    plan=self.plan,
                )
                self.subscription
                self.subscription_start = self.subscription.start
                self.subscription_end = self.subscription.end

        return super().save(*args, **kwargs)

    @classmethod
    def get_last_successful(cls, user: AbstractBaseUser) -> Optional[SubscriptionPayment]:
        return cls.objects.filter(
            user=user,
            status=SubscriptionPayment.Status.COMPLETED,
        ).order_by('created').last()


class SubscriptionPaymentRefund(AbstractTransaction):
    original_payment = models.ForeignKey(SubscriptionPayment, on_delete=models.PROTECT, related_name='refunds')
    # TODO: add support by providers


class Tax(models.Model):
    subscription_payment = models.ForeignKey(SubscriptionPayment, on_delete=models.PROTECT, related_name='taxes')
    amount = MoneyField()

    def __str__(self) -> str:
        return f'{self.amount}'
