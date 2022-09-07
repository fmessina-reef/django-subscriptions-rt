# Generated by Django 4.0.3 on 2022-07-28 19:19

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('subscriptions', '0013_alter_subscription_uid_alter_subscriptionpayment_uid_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='subscriptionpayment',
            name='subscription_uid',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='subscriptions.subscription', to_field='uid'),
        ),
        migrations.AddField(
            model_name='subscriptionpaymentrefund',
            name='original_payment_uid',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='subscriptions.subscriptionpayment', to_field='uid'),
        ),
        migrations.AddField(
            model_name='tax',
            name='subscription_payment_uid',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='subscriptions.subscriptionpayment', to_field='uid'),
        ),
    ]