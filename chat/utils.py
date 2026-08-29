"""Helpers for inspecting uploaded attachments."""
from PIL import Image

from chat.models import Attachment


def describe_upload(upload):
    """Work out what an uploaded file actually is.

    The declared content type is whatever the client felt like sending, so
    the image branch is decided by trying to decode the file rather than by
    trusting the header. Dimensions let a client reserve layout space before
    the image loads.
    """
    width, height = image_dimensions(upload)
    is_image = width is not None

    return {
        'kind': Attachment.Kind.IMAGE if is_image else Attachment.Kind.FILE,
        'original_name': (getattr(upload, 'name', '') or '')[:255],
        'content_type': (getattr(upload, 'content_type', '') or '')[:120],
        'size': getattr(upload, 'size', 0) or 0,
        'width': width,
        'height': height,
    }


def image_dimensions(upload):
    """(width, height) if the file decodes as an image, else (None, None)."""
    try:
        upload.seek(0)
        with Image.open(upload) as image:
            return image.width, image.height
    except Exception:
        # Not an image, truncated, or a format Pillow does not know. Either
        # way it is handled as a plain file.
        return None, None
    finally:
        # The file still has to be readable by the storage backend.
        upload.seek(0)
