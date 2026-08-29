"""The single response shape every API endpoint answers with.

A client only ever parses one envelope::

    {"code": 1,  "message": "Login successful.", "data": {...}}   # success
    {"code": -1, "message": "email: ...",        "data": {...}}   # failure

`code` is 1 on success and -1 on failure, per the project convention.

The HTTP status code is deliberately left alone (201, 400, 401, ...) so that
proxies, load balancers, Sentry and `runserver`'s own request log stay
meaningful. Clients switch on `code`; infrastructure switches on the status.

A view sets its success text with a `success_message` attribute, or a
`success_messages` dict when one view serves several actions or methods::

    class UserViewSet(viewsets.GenericViewSet):
        success_message = 'Request successful.'
        success_messages = {
            'register': 'Account created successfully.',   # by action
            'me:PATCH': 'Account updated.',                # by action + method
            'GET': 'Fetched successfully.',                # by method
        }

Lookup order is action+method, then action, then method, then the default.

Anything not covered falls back to a sensible per-method default, so a new
view is never obliged to say anything at all.
"""
from rest_framework.renderers import JSONRenderer

SUCCESS_CODE = 1
ERROR_CODE = -1

# Used when a view does not declare its own success_message.
DEFAULT_SUCCESS_MESSAGES = {
    'GET': 'Fetched successfully.',
    'POST': 'Created successfully.',
    'PUT': 'Updated successfully.',
    'PATCH': 'Updated successfully.',
    'DELETE': 'Deleted successfully.',
}
FALLBACK_SUCCESS_MESSAGE = 'Request successful.'
FALLBACK_ERROR_MESSAGE = 'Something went wrong.'

# DRF reports errors that belong to no single field under this key.
NON_FIELD_KEYS = ('non_field_errors', 'detail')


class EnvelopeJSONRenderer(JSONRenderer):
    """Wrap every JSON response in the {code, message, data} envelope."""

    def render(self, data, accepted_media_type=None, renderer_context=None):
        renderer_context = renderer_context or {}
        response = renderer_context.get('response')

        # No response in context (rare, e.g. rendering a bare payload), or a
        # payload that is already an envelope: leave it exactly as it is.
        if response is None or self._is_envelope(data):
            return super().render(data, accepted_media_type, renderer_context)

        if response.status_code < 400:
            payload = {
                'code': SUCCESS_CODE,
                'message': self._success_message(renderer_context),
                'data': data,
            }
        else:
            payload = {
                'code': ERROR_CODE,
                'message': self._error_message(data),
                'data': self._error_data(data),
            }

        return super().render(payload, accepted_media_type, renderer_context)

    @staticmethod
    def _is_envelope(data):
        return isinstance(data, dict) and 'code' in data and 'message' in data

    @staticmethod
    def _success_message(renderer_context):
        """Most specific wins: the response, then the view, then the default."""
        response = renderer_context.get('response')
        view = renderer_context.get('view')
        request = renderer_context.get('request')
        method = getattr(request, 'method', '') or ''

        # A view that needs a per-call message sets it on the response object.
        message = getattr(response, 'message', None)
        if message:
            return message

        # A viewset serves several actions over the same HTTP method, so
        # look up the action first: 'register', 'login', and 'me:PATCH' when
        # one action needs to differ per method.
        action = getattr(view, 'action', None)
        messages = getattr(view, 'success_messages', None) or {}
        for key in (f'{action}:{method}', action, method):
            if key and key in messages:
                return messages[key]

        message = getattr(view, 'success_message', None)
        if message:
            return message

        return DEFAULT_SUCCESS_MESSAGES.get(method, FALLBACK_SUCCESS_MESSAGE)

    @classmethod
    def _error_message(cls, data):
        """Pull one human-readable line out of a DRF error payload.

        DRF hands back shapes like ``{'detail': '...'}``, ``{'email':
        ['...']}`` or a bare list. The client shows this string; the full
        per-field detail stays available under `data`.
        """
        if isinstance(data, dict):
            for key in NON_FIELD_KEYS:
                if key in data:
                    text = cls._first_string(data[key])
                    if text:
                        return text
            for field, value in data.items():
                text = cls._first_string(value)
                if text:
                    return f'{field}: {text}'

        return cls._first_string(data) or FALLBACK_ERROR_MESSAGE

    @classmethod
    def _first_string(cls, value):
        """Depth-first search for the first usable string in a DRF error."""
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            for item in value:
                text = cls._first_string(item)
                if text:
                    return text
            return None
        if isinstance(value, dict):
            for item in value.values():
                text = cls._first_string(item)
                if text:
                    return text
            return None
        # ErrorDetail subclasses str, so anything left is a stray object.
        return str(value) if value is not None else None

    @staticmethod
    def _error_data(data):
        """Keep per-field errors so a form can highlight the offending input.

        ``{'detail': '...'}`` carries nothing a field could use, so it
        collapses to null rather than repeating the message.
        """
        if isinstance(data, dict):
            if set(data.keys()) <= {'detail'}:
                return None
            return data
        return data
