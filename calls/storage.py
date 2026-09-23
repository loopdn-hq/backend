"""The recordings bucket: signing download links and deleting files.

LiveKit's egress service WRITES the file; Django only ever reads its address
and removes it. Kept here, away from the models and services, so a test patches
two functions and never a boto3 client.

Credentials come from the ECS task role, as they do for media uploads
(loopd/settings.py, STORAGES) - no keys are read from the environment.
"""
import logging

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

from calls.livekit import CallServiceError

logger = logging.getLogger(__name__)


def _client(region):
    return boto3.client('s3', region_name=region)


def download_url(recording):
    """A link that downloads this recording, good for a few minutes.

    Signed per request rather than stored: the bucket is private and the link
    is the credential, so it must not outlive the moment somebody passed the
    permission check for it.
    """
    extension = recording.s3_key.rsplit('.', 1)[-1]
    try:
        return _client(settings.LIVEKIT_RECORDING_REGION).generate_presigned_url(
            'get_object',
            Params={
                'Bucket': recording.s3_bucket,
                'Key': recording.s3_key,
                # Saved as a file with a sensible name rather than played in a tab.
                'ResponseContentDisposition': (
                    f'attachment; filename="call-{recording.call_room_id}'
                    f'-recording-{recording.pk}.{extension}"'
                ),
            },
            ExpiresIn=settings.LIVEKIT_RECORDING_URL_TTL_SECONDS,
        )
    except (BotoCoreError, ClientError) as exc:
        logger.exception('Could not sign a link for recording %s', recording.pk)
        raise CallServiceError(
            'The recording is not available right now. Try again in a moment.'
        ) from exc


def delete_file(recording):
    """Remove the recording's object. Deleting an absent one is not an error.

    S3 answers a delete of a missing key with success, which is what a retry
    after a half-finished delete needs.
    """
    try:
        _client(settings.LIVEKIT_RECORDING_REGION).delete_object(
            Bucket=recording.s3_bucket, Key=recording.s3_key
        )
    except (BotoCoreError, ClientError) as exc:
        logger.exception('Could not delete the file of recording %s', recording.pk)
        raise CallServiceError(
            'The recording could not be deleted right now. Try again in a moment.'
        ) from exc
