# Generated by Django 4.1.3 on 2023-07-11 06:14
from datetime import timedelta
import logging
from django.utils.timezone import now

from more_itertools import pairwise
from django.db import migrations
from django.conf import settings

log = logging.getLogger(__name__)


def fix_default_subscriptions(apps, schema_editor):
    from subscriptions.functions import get_default_plan
    from subscriptions.models import MAX_DATETIME

    User = apps.get_model(*settings.AUTH_USER_MODEL.rsplit('.', maxsplit=1))

    if not (default_plan := get_default_plan()):
        return

    for user in User.objects.all():
        subscriptions = user.subscriptions.filter(plan=default_plan).order_by('start', 'end')
        for sub1, sub2 in pairwise(subscriptions):

            # # swallow
            if sub2.start <= sub1.start and sub1.end <= sub2.end:
                log.debug('Swallowed:\n%s\n%s', sub1, sub2)

                payments = sub1.payments.all()
                for payment in payments:
                    assert payment.amount.amount == 0, f'Non-zero payment: {payment}'
                payments.delete()
                sub1.delete()

            # merge
            elif sub1.end >= sub2.start:
                log.debug('Merging:\n%s\n%s', sub1, sub2)
                sub2.start = sub1.start
                sub2.save()

                try:
                    payments = sub1.payments.all()
                    for payment in payments:
                        assert payment.amount.amount == 0, f'Non-zero payment: {payment}'
                    payments.delete()
                    sub1.delete()
                except AssertionError:
                    log.exception('Could not delete %s', sub1)

        # extend last subscription
        last_subscription = subscriptions.last()
        if (
            last_subscription
            and last_subscription.end > now() + timedelta(days=365*5)
            and last_subscription.end != MAX_DATETIME
        ):
            log.debug('Extending last default subscription: %s', last_subscription)
            last_subscription.end = MAX_DATETIME
            last_subscription.save()


class Migration(migrations.Migration):
    dependencies = [
        ("subscriptions", "0035_alter_subscription_auto_prolong"),
    ]

    operations = [
        migrations.RunPython(fix_default_subscriptions),
    ]