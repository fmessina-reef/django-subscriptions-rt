import base64
import dataclasses
import datetime
import enum
import pathlib
from typing import (
    Any,
    Optional,
)

import jwt
from OpenSSL import crypto
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)
from cryptography.x509 import load_der_x509_certificate
from django.conf import settings

from .api import (
    AppleEnvironment,
    datetime_from_ms_timestamp,
)

CACHED_APPLE_ROOT_CERT: Optional[crypto.X509] = None


class PayloadValidationError(Exception):
    pass


def load_certificate_from_bytes(certificate_data: bytes) -> crypto.X509:
    basic_cert = load_der_x509_certificate(certificate_data)
    # OpenSSL can load ASN1 and PEM, cryptography can load DER. Luckily, OpenSSL can use cryptography.
    return crypto.X509.from_cryptography(basic_cert)


def load_certificate_from_x5c(x5c_entry: str) -> crypto.X509:
    # Each string in the array is a base64-encoded (not base64url-encoded) DER [ITU.X690.2008] PKIX certificate value.
    certificate_data = base64.b64decode(x5c_entry)
    return load_certificate_from_bytes(certificate_data)


def get_original_apple_certificate() -> crypto.X509:
    global CACHED_APPLE_ROOT_CERT

    if CACHED_APPLE_ROOT_CERT is None:
        cert_path = pathlib.Path(settings.APPLE_ROOT_CERTIFICATE_PATH)
        if not cert_path.exists() or not cert_path.is_file():
            raise ValueError('No root certificate for Apple provided. Check Django configuration settings.')

        CACHED_APPLE_ROOT_CERT = load_certificate_from_bytes(cert_path.read_bytes())

    return CACHED_APPLE_ROOT_CERT


def are_certificates_identical(cert_1: crypto.X509, cert_2: crypto.X509) -> bool:
    # There are a few ways to compare these, but checking their binary representation seems good enough.
    return cert_1.to_cryptography().public_bytes(Encoding.DER) == cert_2.to_cryptography().public_bytes(Encoding.DER)


def validate_and_fetch_apple_signed_payload(signed_payload: str) -> dict[str, Any]:
    """
    Information about the JWT header from this payload.
    https://developer.apple.com/documentation/appstoreservernotifications/jwsdecodedheader

    This consists of three steps:
    1. Checking whether the root certificate is the same as the Apple root certificate.
    2. Checking whether the certificate chain "works as intended" (root signed intermediate, store signed final one).
    3. Using public key from the last certificate to validate the JWT payload.

    Things to look after
    https://mail.python.org/pipermail/cryptography-dev/2016-August/000676.html
    """
    header = jwt.get_unverified_header(signed_payload)

    # Load certificates chain.
    certificates_chain = header['x5c']
    if not isinstance(certificates_chain, list) or len(certificates_chain) == 0:
        raise PayloadValidationError('Invalid certificate chain format or '
                                     'no certificates provided in the certificate chain.')

    # Fetch first certificate and confirm that it's the same as the Apple root certificate.
    apple_root_certificate = get_original_apple_certificate()

    root_certificate_x5c = certificates_chain[-1]  # Root should be at the end, according to docs.
    root_certificate = load_certificate_from_x5c(root_certificate_x5c)

    if not are_certificates_identical(apple_root_certificate, root_certificate):
        raise PayloadValidationError('Root certificate differs from the Apple certificate.')

    # Check that the whole certificate chain is valid.
    certificate_store = crypto.X509Store()
    certificate_store.add_cert(apple_root_certificate)

    current_certificate: Optional[crypto.X509] = None

    # Go from the back, excluding the one that we've already validated.
    # NOTE(kkalinowski): While it could be done with a single X509StoreContext,
    # using untrusted cert list, I'm unsure about safety of this method (and far from understanding it enough
    # to be able to determine this myself). The one presented below is said to be secure by someone smarter than me.
    for certificate_x5c in reversed(certificates_chain[:-1]):
        current_certificate = load_certificate_from_x5c(certificate_x5c)

        # Verify whether this certificate is valid.
        context = crypto.X509StoreContext(certificate_store, current_certificate)
        try:
            context.verify_certificate()
        except crypto.X509StoreContextError:
            # TODO(kkalinowski): consider more information in this place.
            raise PayloadValidationError('Validation of one of certificates failed.')

        # Add it to the store.
        certificate_store.add_cert(current_certificate)

    # Fetch public key from the last certificate and validate the payload.
    algorithm = header['alg']
    # TODO(kkalinowski): validate whether this is the correct set of parameters used as the secret
    #  from the actual app-store query. While encoding should be fine (all other keys are in DER fromat)
    #  the PublicFormat has SubjectPublicKeyInfo entry, that "could be the right one".
    secret = current_certificate.to_cryptography().public_key().public_bytes(Encoding.DER, PublicFormat.PKCS1)

    try:
        payload = jwt.decode(signed_payload, secret, algorithm)
    except jwt.PyJWTError as ex:
        raise PayloadValidationError(str(ex))

    return payload


@enum.unique
class AppStoreNotificationTypeV2(str, enum.Enum):
    """
    https://developer.apple.com/documentation/appstoreservernotifications/notificationtype
    """
    CONSUMPTION_REQUEST = 'CONSUMPTION_REQUEST'
    DID_CHANGE_RENEWAL_PREF = 'DID_CHANGE_RENEWAL_PREF'
    DID_CHANGE_RENEWAL_STATUS = 'DID_CHANGE_RENEWAL_STATUS'
    DID_FAIL_TO_RENEW = 'DID_FAIL_TO_RENEW'
    DID_RENEW = 'DID_RENEW'
    EXPIRED = 'EXPIRED'
    GRACE_PERIOD_EXPIRED = 'GRACE_PERIOD_EXPIRED'
    OFFER_REDEEMED = 'OFFER_REDEEMED'
    PRICE_INCREASE = 'PRICE_INCREASE'
    REFUND = 'REFUND'
    REFUND_DECLINED = 'REFUND_DECLINED'
    RENEWAL_EXTENDED = 'RENEWAL_EXTENDED'
    REVOKE = 'REVOKE'
    SUBSCRIBED = 'SUBSCRIBED'
    TEST = 'TEST'


@enum.unique
class AppStoreNotificationTypeV2Subtype(str, enum.Enum):
    """
    https://developer.apple.com/documentation/appstoreservernotifications/subtype
    """
    INITIAL_BUY = 'INITIAL_BUY'
    RESUBSCRIBE = 'RESUBSCRIBE'
    DOWNGRADE = 'DOWNGRADE'
    UPGRADE = 'UPGRADE'
    AUTO_RENEW_ENABLED = 'AUTO_RENEW_ENABLED'
    AUTO_RENEW_DISABLED = 'AUTO_RENEW_DISABLED'
    VOLUNTARY = 'VOLUNTARY'
    BILLING_RETRY = 'BILLING_RETRY'
    PRICE_INCREASE = 'PRICE_INCREASE'
    GRACE_PERIOD = 'GRACE_PERIOD'
    BILLING_RECOVERY = 'BILLING_RECOVERY'
    PENDING = 'PENDING'
    ACCEPTED = 'ACCEPTED'


@dataclasses.dataclass
class AppStoreTransactionInfo:
    # UUID set by the application, empty if not set.
    app_account_token: str
    bundle_id: str

    purchase_date: datetime.datetime
    expires_date: datetime.datetime

    product_id: str
    transaction_id: str
    original_transaction_id: str

    @classmethod
    def from_signed_payload(cls, signed_payload_data: str) -> 'AppStoreTransactionInfo':
        payload = validate_and_fetch_apple_signed_payload(signed_payload_data)

        return cls(
            app_account_token=payload['appAccountToken'],
            bundle_id=payload['bundleId'],

            purchase_date=datetime_from_ms_timestamp(payload['purchaseDate']),
            expires_date=datetime_from_ms_timestamp(payload['expiresDate']),

            product_id=payload['productId'],
            transaction_id=payload['transactionId'],
            original_transaction_id=payload['originalTransactionId'],
        )


@dataclasses.dataclass
class AppStoreNotificationData:
    """
    https://developer.apple.com/documentation/appstoreservernotifications/data
    """
    app_apple_id: int
    bundle_id: str
    bundle_version: str
    environment: AppleEnvironment

    # Renewal doesn't seem to carry anything interesting.
    transaction_info: AppStoreTransactionInfo

    @classmethod
    def from_json(cls, json_dict: dict) -> 'AppStoreNotificationData':
        return cls(
            app_apple_id=json_dict['appAppleId'],
            bundle_id=json_dict['bundleId'],
            bundle_version=json_dict['bundleVersion'],
            environment=AppleEnvironment(json_dict['environment']),
            transaction_info=AppStoreTransactionInfo.from_signed_payload(json_dict['signedTransactionInfo']),
        )


@dataclasses.dataclass
class AppStoreNotification:
    """
    https://developer.apple.com/documentation/appstoreservernotifications/responsebodyv2decodedpayload
    """
    notification: AppStoreNotificationTypeV2
    subtype: AppStoreNotificationTypeV2Subtype
    # Used to deduplicate notifications.
    notification_uuid: str

    data: AppStoreNotificationData

    @property
    def transaction_info(self) -> AppStoreTransactionInfo:
        return self.data.transaction_info

    @classmethod
    def from_signed_payload(cls, signed_payload_data: str) -> 'AppStoreNotification':
        payload = validate_and_fetch_apple_signed_payload(signed_payload_data)

        return cls(
            notification=AppStoreNotificationTypeV2(payload['notification']),
            subtype=AppStoreNotificationTypeV2Subtype(payload['subtype']),
            notification_uuid=payload['notificationUUID'],
            data=AppStoreNotificationData.from_json(payload['data'])
        )
