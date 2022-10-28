from contextlib import suppress
from dataclasses import dataclass
from logging import getLogger
from typing import (
    Callable,
    ClassVar,
    Iterable,
    Optional,
    Tuple,
)

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import User
from django.core.exceptions import SuspiciousOperation
from django.db import transaction
from pydantic import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.status import (
    HTTP_200_OK,
    HTTP_400_BAD_REQUEST,
)

from subscriptions.models import (
    Plan,
    Subscription,
    SubscriptionPayment,
)
from .api import (
    AppleAppStoreAPI,
    AppleInApp,
    AppleLatestReceiptInfo,
    AppleReceiptRequest,
    AppleVerifyReceiptResponse,
)
from .app_store import (
    AppStoreNotification,
    AppStoreNotificationTypeV2,
    AppStoreNotificationTypeV2Subtype,
    AppleAppStoreNotification,
    PayloadValidationError,
    get_original_apple_certificate,
)
from .exceptions import (
    AppleInvalidOperation,
    AppleReceiptValidationError,
    AppleSubscriptionNotCompletedError,
    InvalidAppleReceiptError,
)
from .. import Provider
from ...api.serializers import SubscriptionPaymentSerializer

logger = getLogger(__name__)


@dataclass
class AppleInAppProvider(Provider):
    # This is also name of the field in metadata of the Plan, that stores Apple App Store product id.
    codename: ClassVar[str] = 'apple_in_app'
    bundle_id: ClassVar[str] = settings.APPLE_BUNDLE_ID
    api: AppleAppStoreAPI = None

    def __post_init__(self):
        self.api = AppleAppStoreAPI(settings.APPLE_SHARED_SECRET)
        # Check whether the Apple certificate is provided and is a valid certificate.
        get_original_apple_certificate()

    def charge_online(self, user: AbstractBaseUser, plan: Plan, subscription: Optional[Subscription] = None,
                      quantity: int = 1) -> Tuple[SubscriptionPayment, str]:
        """
        In case of in-app purchase this operation is triggered from the mobile application library.
        """
        raise AppleInvalidOperation()

    def charge_offline(self, user: AbstractBaseUser, plan: Plan, subscription: Optional[Subscription] = None,
                       quantity: int = 1,
                       reference_payment: Optional[SubscriptionPayment] = None) -> SubscriptionPayment:
        raise AppleInvalidOperation()

    def webhook(self, request: Request, payload: dict) -> Response:
        handlers = {
            AppleReceiptRequest: self._handle_receipt,
            AppleAppStoreNotification: self._handle_app_store,
        }

        validation_error_messages = []

        for request_class, handler in handlers.items():
            try:
                instance = request_class.parse_obj(payload)
                run_handler = handler
                # If we find a matching object, stop performing operations in context of this try-except.
                break
            except ValidationError as validation_error:
                validation_error_messages.append(str(validation_error))
        else:
            # Came to an end without breaking.
            logger.error('Failed matching the payload to any registered request:\n%s.',
                         '\n\n'.join(validation_error_messages))
            return Response(status=HTTP_400_BAD_REQUEST)

        return run_handler(request, instance)

    def check_payments(self, payments: Iterable[SubscriptionPayment]):
        for payment in payments:
            if payment.status != SubscriptionPayment.Status.COMPLETED:
                # All the operations that we care about should be completed before they reach us.
                raise AppleSubscriptionNotCompletedError(payment.provider_transaction_id)

    @classmethod
    def _raise_if_invalid(cls, response: AppleVerifyReceiptResponse) -> None:
        if not response.is_valid or response.receipt.bundle_id != cls.bundle_id:
            raise AppleReceiptValidationError()

    def _get_plan_for_product_id(self, product_id: str) -> Plan:
        search_kwargs = {
            f'metadata__{self.codename}': product_id,
        }
        return Plan.objects.get(**search_kwargs)

    def _handle_single_receipt_info(self,
                                    user: User,
                                    receipt_info: AppleLatestReceiptInfo) -> Optional[SubscriptionPayment]:
        if receipt_info.cancellation_date is not None:
            logger.error('Found a cancellation date in receipt: %s', receipt_info)
            return None

        with suppress(SubscriptionPayment.DoesNotExist):  # noqa (DoesNotExist seems not to be an exception for PyCharm)
            payment = SubscriptionPayment.objects.get(provider_codename=self.codename,
                                                      provider_transaction_id=receipt_info.transaction_id)
            return payment

        # Find the right plan to create subscription.
        try:
            plan = self._get_plan_for_product_id(receipt_info.product_id)
        except Plan.DoesNotExist:
            # This means that something wasn't connected as needed.
            logger.exception('Plan for apple in-app purchase "%s" not found.', receipt_info.product_id)
            return None

        # Create subscription payment. Subscription is created automatically.
        subscription_payment = SubscriptionPayment.objects.create(
            provider_codename=self.codename,
            provider_transaction_id=receipt_info.transaction_id,
            # NOTE(kkalinowski): from my understanding, we can only receive receipt after the person has paid
            # but the transaction (from the perspective of the app) might have not yet finished. Money were spent.
            status=SubscriptionPayment.Status.COMPLETED,
            # In-app purchase doesn't report the money.
            # We mark it as None to indicate we don't know how much did it cost.
            amount=None,
            user=user,
            plan=plan,
            subscription_start=receipt_info.purchase_date,
            # If the cancellation date is set, it means that the user was refunded and for whatever reason
            # we weren't notified about this purchase.
            subscription_end=receipt_info.expires_date,
        )
        subscription_payment.subscription.auto_prolong = False
        subscription_payment.save()

        return subscription_payment

    @transaction.atomic
    def _handle_receipt(self, request: Request, payload: AppleReceiptRequest) -> Response:
        receipt = payload.transaction_receipt

        # Validate the receipt. Fetch the status and product.
        receipt_data = self.api.fetch_receipt_data(receipt)
        self._raise_if_invalid(receipt_data)

        if not receipt_data.latest_receipt_info:  # Either None or empty list.
            raise InvalidAppleReceiptError('No latest receipt info provided, no recurring subscriptions to check.')

        latest_payment = None
        for receipt_info in receipt_data.latest_receipt_info:
            # We receive all the elements and check whether we've actually activated them.
            payment = self._handle_single_receipt_info(request.user, receipt_info)
            if payment is None:
                continue

            if latest_payment is None or payment.subscription_end > latest_payment.subscription_end:
                latest_payment = payment

        if latest_payment is None:
            logger.warning('No subscription information provided in the payload receipt.')
            return Response(status=HTTP_400_BAD_REQUEST)

        return Response(SubscriptionPaymentSerializer(latest_payment).data, status=HTTP_200_OK)

    def _handle_renewal(self, _user: AbstractBaseUser, notification: AppStoreNotification) -> Response:
        transaction_info = notification.transaction_info

        # This gets us the very first transaction that happened for this person.
        subscription_payment = SubscriptionPayment.objects.get(
            provider_codename=self.codename,
            provider_transaction_id=transaction_info.original_transaction_id,
        )

        # Information about this should've been received earlier via change, but downgrades are to happen
        # only at the start of new cycle. So we handle them here.
        current_product_id = subscription_payment.plan.metadata.get(self.codename)
        if current_product_id != transaction_info.product_id:
            subscription_payment.plan = self._get_plan_for_product_id(transaction_info.product_id)
            # Create a new subscription.
            subscription_payment.subscription = None

        subscription_payment.pk = None
        # Updating relevant fields.
        subscription_payment.provider_transaction_id = transaction_info.transaction_id
        subscription_payment.subscription_start = transaction_info.purchase_date
        subscription_payment.subscription_end = transaction_info.expires_date
        subscription_payment.created = None
        subscription_payment.updated = None
        subscription_payment.save()

        return Response(status=HTTP_200_OK)

    def _handle_subscription_change(self, user: AbstractBaseUser, notification: AppStoreNotification) -> Response:
        assert notification.subtype in {
            AppStoreNotificationTypeV2Subtype.UPGRADE,
            AppStoreNotificationTypeV2Subtype.DOWNGRADE,
        }, f'Unsupported notification subtype received for subscription change: {notification.subtype}.'

        transaction_info = notification.transaction_info

        if notification.subtype == AppStoreNotificationTypeV2Subtype.DOWNGRADE:
            logger.info('User %s requested a downgrade to %s. This will happen during the next renewal.',
                        user.get_username(), transaction_info.product_id)
            return Response(status=HTTP_200_OK)

        # We handle this in two steps:
        # 1. We shorten the current subscription and subscription payment to current start of the period.
        # 2. We add a new subscription with a new plan.

        # Find the latest apple payment by this user.
        latest_payment = SubscriptionPayment.objects.filter(
            provider_codename=self.codename,
            user=user,
        ).order_by('subscription_end').last()
        # Purchase date is the time when we should stop the previous subscription.
        previous_payment_end_time = transaction_info.purchase_date

        # Ensuring that subscription ends earlier before making the payment end earlier.
        latest_payment.subscription.end = previous_payment_end_time
        latest_payment.subscription.save()
        latest_payment.subscription_end = previous_payment_end_time
        latest_payment.save()

        # Creating a new subscription with a new plan.
        return self._handle_renewal(user, notification)

    @transaction.atomic
    def _handle_app_store(self, request: Request, payload: AppleAppStoreNotification) -> Response:
        signed_payload = payload.signed_payload

        try:
            notification_object = AppStoreNotification.from_signed_payload(signed_payload)
        except PayloadValidationError as exception:
            logger.exception('Invalid payload received from the notification endpoint: "%s"', signed_payload)
            raise SuspiciousOperation() from exception

        notification_handling: dict[AppStoreNotificationTypeV2,
                                    Callable[[AbstractBaseUser, AppStoreNotification], Response]] = {
            AppStoreNotificationTypeV2.DID_RENEW: self._handle_renewal,
            AppStoreNotificationTypeV2.DID_CHANGE_RENEWAL_PREF: self._handle_subscription_change,
        }

        # We're only handling a handful of events. The rest means that,
        # for whatever reason, it failed, or we don't care about them for now.
        # As for expirations – these are handled on our side anyway, that would be only an additional validation.
        # In all other cases we're just returning "200 OK" to let the App Store know that we're received the message.
        handler = notification_handling.get(notification_object.notification, None)
        if handler is None:
            logger.info('Received apple notification %s (%s) and ignored it. Payload: %s',
                        notification_object.notification,
                        notification_object.subtype,
                        str(payload))
            return Response(status=HTTP_200_OK)

        return handler(request.user, notification_object)
