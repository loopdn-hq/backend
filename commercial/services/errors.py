"""What the campaign services refuse with. The views turn these into answers.

Two kinds, because a client handles them differently: `Invalid` is input that
breaks a rule, field by field, which a form shows beside its fields (400);
`Refused` is a well-formed request the campaign's state does not allow - a
submitted campaign cannot be edited - which a page shows as one message (409).
"""


class Refused(Exception):
    message = "That isn't possible for this campaign now."

    def __init__(self, message=None):
        if message:
            self.message = message
        super().__init__(self.message)


class Invalid(Exception):
    """`errors` is {field: message}, keyed by the API's field names."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__(errors)
