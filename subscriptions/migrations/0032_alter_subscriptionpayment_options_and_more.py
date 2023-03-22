# Generated by Django 4.0.3 on 2023-03-21 10:14
from collections import defaultdict

import djmoney.models.fields
from django.db import migrations, models


def remove_duplicates_on_transaction(model):
    """
    The main idea is:
    - fetch all objects;
    - group them by provider and transaction id combo;
    - check all subscriptions and how many models they have assigned to them;
    - leave a single subscription with all the models attached to it, purge everything else.

    Assumptions:
    - single transaction id / codename pair is unique and assigned to a single user (we're not checking it);
    - single transaction id / codename pair belongs to a single plan, just multiple entries.
    """
    all_entries = model.objects.prefetch_related('subscription').all()

    grouped_entries = defaultdict(list)
    for entry in all_entries:
        # we don't want to touch entries with empty transaction IDs
        # because they are not duplucates
        if entry.provider_transaction_id is None:
            continue

        key = (entry.provider_codename, entry.provider_transaction_id)
        grouped_entries[key].append(entry)

    # Gather problematic entries.
    grouped_subscriptions = defaultdict(list)
    key_to_subscription_uid = defaultdict(set)

    for key, entry_list in grouped_entries.items():
        # Map all subscriptions to all entries assigned to them.
        for entry in entry_list:
            grouped_subscriptions[entry.subscription.uid].append(entry)

        # Single entry – no issue.
        if len(entry_list) == 1:
            continue

        for entry in entry_list:
            key_to_subscription_uid[key].add(entry.subscription.uid)

    for key, subscription_uids in key_to_subscription_uid.items():
        # Sort subscriptions by amount of children, descending.
        subscription_to_children_count = {
            sub_uid: len(grouped_subscriptions[sub_uid])
            for sub_uid in subscription_uids
        }
        # Picking the first element (uid) of the first element of the sorted list.
        wanted_uid = sorted(list(subscription_to_children_count.items()), key=lambda x: x[1], reverse=True)[0][0]

        # Pick one, purge the rest.
        for subscription_uid in subscription_uids:
            if subscription_uid == wanted_uid:
                continue

            children = grouped_subscriptions[subscription_uid]
            subscription_obj = None
            for child in children:
                key = (child.provider_codename, child.provider_transaction_id)
                key_to_subscription_uid[key].remove(subscription_uid)
                subscription_obj = child.subscription
                child.delete()

            assert subscription_obj is not None
            subscription_obj.delete()


def remove_subscription_duplicates(apps, scheme_editor):  # noqa (unused scheme_editor)
    models_inheriting_from_abstract_transaction = ['SubscriptionPayment', 'SubscriptionPaymentRefund']

    for model_name in models_inheriting_from_abstract_transaction:
        model = apps.get_model('subscriptions', model_name)
        remove_duplicates_on_transaction(model)


def remove_null_transaction_ids(apps, scheme_editor):  # noqa (unused scheme_editor)
    models_inheriting_from_abstract_transaction = ['SubscriptionPayment', 'SubscriptionPaymentRefund']

    for model_name in models_inheriting_from_abstract_transaction:
        model = apps.get_model('subscriptions', model_name)
        model.objects.filter(provider_transaction_id=None).all().delete()


def no_op(apps, scheme_editor):  # noqa (unused parameters)
    # Left empty intentionally. Required for testing purposes.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('subscriptions', '0031_alter_plan_tier'),
    ]

    operations = [
        migrations.RunPython(remove_subscription_duplicates, no_op),
        migrations.AddConstraint(
            model_name='subscriptionpayment',
            constraint=models.UniqueConstraint(fields=('provider_codename', 'provider_transaction_id'), name='unique_subscription_payment'),
        ),
        migrations.AddConstraint(
            model_name='subscriptionpaymentrefund',
            constraint=models.UniqueConstraint(fields=('provider_codename', 'provider_transaction_id'), name='unique_subscription_payment_refund'),
        ),
    ]
