"""Helpers for inspecting uploaded attachments."""
from pathlib import PurePosixPath

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


# Audio by name, for an upload whose declared type says nothing useful - an
# audio file sent as application/octet-stream is still audio. `.webm` is left
# out on purpose: it is as often a video, and the app's own recordings declare
# audio/webm, so the type catches those.
AUDIO_EXTENSIONS = frozenset({
    '.aac', '.amr', '.flac', '.m4a', '.mp3', '.oga', '.ogg', '.opus', '.wav',
    '.weba',
})


def is_audio(content_type, name):
    """Whether a file is a voice note or other audio.

    The same test the web app uses to play one inline - a declared `audio/*`
    type - plus the extension, for an upload whose declared type is generic.
    Deliberately a declared-type check: a file disguised as something else does
    not play as a voice note either, it shows as a plain file.
    """
    if (content_type or '').lower().startswith('audio/'):
        return True
    return PurePosixPath(name or '').suffix.lower() in AUDIO_EXTENSIONS
