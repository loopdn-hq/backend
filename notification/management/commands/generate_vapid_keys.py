"""Make the one key pair web push needs.

Run once per environment. The pair identifies THIS server to every browser push
service; rotating it invalidates every existing subscription, so staging and
production each keep their own and neither changes without a reason.
"""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Generate a VAPID key pair for web push.'

    def handle(self, *args, **options):
        private_key = ec.generate_private_key(ec.SECP256R1())

        # The raw 32-byte private value, base64url with no padding: what
        # py-vapid (inside pywebpush) reads from VAPID_PRIVATE_KEY.
        private_value = private_key.private_numbers().private_value
        private_raw = private_value.to_bytes(32, 'big')

        # The uncompressed public point, 65 bytes: what a browser passes as
        # `applicationServerKey` when it subscribes.
        public_raw = private_key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('VAPID key pair generated.'))
        self.stdout.write('')
        self.stdout.write(f'VAPID_PUBLIC_KEY={_b64(public_raw)}')
        self.stdout.write(f'VAPID_PRIVATE_KEY={_b64(private_raw)}')
        self.stdout.write('')
        self.stdout.write(
            'Keep the private key secret - in SSM on a deployment, in .env on '
            'a laptop. The public key is handed to every browser and is not '
            'sensitive. Changing either one unsubscribes everybody.'
        )


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')
