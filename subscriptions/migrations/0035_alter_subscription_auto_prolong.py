# Generated by Django 4.1.3 on 2023-07-06 14:06

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("subscriptions", "0034_alter_subscription_initial_charge_offset"),
    ]

    operations = [
        migrations.AlterField(
            model_name="subscription",
            name="auto_prolong",
            field=models.BooleanField(),
        ),
    ]
