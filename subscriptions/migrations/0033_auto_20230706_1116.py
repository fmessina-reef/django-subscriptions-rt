# Generated by Django 4.1.3 on 2023-07-06 11:16

from django.db import migrations


def disable_default_plan_auto_prolong(apps, schema_editor):
    db_alias = schema_editor.connection.alias
    Plan = apps.get_model('subscriptions', 'Plan')
    Subscription = apps.get_model('subscriptions', 'Subscription')

    try:
        from constance import config
    except ImportError:
        return

    default_plan_id = config.SUBSCRIPTIONS_DEFAULT_PLAN_ID
    default_plan = Plan.objects.using(db_alias).filter(id=default_plan_id).first()
    if not default_plan:
        return

    Subscription.objects.using(db_alias).filter(plan_id=default_plan_id, auto_prolong=True).update(auto_prolong=False)


class Migration(migrations.Migration):
    dependencies = [
        ("subscriptions", "0032_subscription_initial_charge_offset_and_more"),
    ]

    operations = [
        migrations.RunPython(disable_default_plan_auto_prolong),
    ]