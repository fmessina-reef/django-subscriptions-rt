# Generated by Django 4.0.3 on 2022-11-03 14:22

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('subscriptions', '0025_feature_tier_plan_tier'),
    ]

    operations = [
        migrations.AlterField(
            model_name='subscriptionpayment',
            name='status',
            field=models.PositiveSmallIntegerField(choices=[(0, 'Pending'), (1, 'Preauth'), (2, 'Completed'), (3, 'Cancelled'), (4, 'Error')], default=0),
        ),
        migrations.AlterField(
            model_name='subscriptionpaymentrefund',
            name='status',
            field=models.PositiveSmallIntegerField(choices=[(0, 'Pending'), (1, 'Preauth'), (2, 'Completed'), (3, 'Cancelled'), (4, 'Error')], default=0),
        ),
    ]