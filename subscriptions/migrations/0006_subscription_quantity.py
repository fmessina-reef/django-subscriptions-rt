# Generated by Django 4.0.3 on 2022-06-06 13:50

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('subscriptions', '0005_alter_subscriptionpayment_metadata_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='subscription',
            name='quantity',
            field=models.PositiveIntegerField(default=1),
        ),
    ]