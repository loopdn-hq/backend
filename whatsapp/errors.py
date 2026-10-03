"""Errors the WhatsApp API answers with. Pure, so loopd/exceptions.py can import it early."""


class ConnectionRefused(Exception):
    """The request cannot be done as asked. The message is safe to show.

    `code` is stable and machine-readable - the web app branches on it, most
    importantly `whatsapp_not_connected`, which is its cue to open Embedded
    Signup before retrying an invitation.
    """

    def __init__(self, message, *, code='invalid'):
        super().__init__(message)
        self.code = code
