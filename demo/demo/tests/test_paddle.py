from datetime import datetime, timedelta

from django.utils.timezone import now
from freezegun import freeze_time
from subscriptions.models import Subscription
from subscriptions.providers import get_provider
from subscriptions.providers.paddle import PaddleProvider


def test_provider(paddle):
    assert isinstance(get_provider(), PaddleProvider)


def test_subscription(paddle, plan, user_client):
    response = user_client.post('/api/subscribe/', {'plan': plan.id})
    assert response.status_code == 200, response.content
    assert 'paddle.com' in response.json()['redirect_url']


def test_webhook(paddle, client, user_client, unconfirmed_payment, paddle_webhook_payload):
    response = user_client.get('/api/subscriptions/')
    assert response.status_code == 200, response.content
    assert len(response.json()) == 0

    webhook_time = now() + timedelta(hours=2)
    with freeze_time(webhook_time):
        response = client.post('/api/webhook/paddle/', paddle_webhook_payload)
        assert response.status_code == 200, response.content

    with freeze_time(webhook_time + timedelta(hours=1)):
        response = user_client.get('/api/subscriptions/')
        assert response.status_code == 200, response.content
        subscriptions = response.json()
        assert len(subscriptions) == 1

        # check that subscription started when webhook arrived
        subscription = subscriptions[0]
        start = datetime.fromisoformat(subscription['start'].replace('Z', '+00:00'))
        assert start - webhook_time < timedelta(seconds=10)

        # check that subscription lasts as much as stated in plan description
        end = datetime.fromisoformat(subscription['end'].replace('Z', '+00:00'))
        assert start + unconfirmed_payment.plan.charge_period == end


def test_webhook_idempotence(paddle, client, unconfirmed_payment, paddle_webhook_payload):
    assert not Subscription.objects.all().exists()

    response = client.post('/api/webhook/paddle/', paddle_webhook_payload)
    assert response.status_code == 200, response.content
    start_old, end_old = Subscription.objects.values_list('start', 'end').last()

    response = client.post('/api/webhook/paddle/', paddle_webhook_payload)
    assert response.status_code == 200, response.content
    start_new, end_new = Subscription.objects.values_list('start', 'end').last()

    assert start_old == start_new
    assert end_old == end_new