"""Generate the v2 Postman collection from one description of every endpoint.

Written as a generator rather than hand-authored JSON because the collection is
~70KB of deeply nested objects: by hand, a request drifts from the code the day
somebody renames a field, and nothing catches it. Here every request is a few
lines and the shape is produced once.

Run:  python postman/build_v2.py
"""
import json
import pathlib

BASE = '{{base_url}}'
OUT = pathlib.Path(__file__).parent / 'loopd_v2.postman_collection.json'


def url(path):
    """A Postman url object from '/api/v1/...' with {{vars}} left intact."""
    clean = path.strip('/')
    return {
        'raw': f'{BASE}/{clean}',
        'host': [BASE],
        'path': clean.split('/'),
    }


def req(name, method, path, *, body=None, desc='', auth=True, tests=None,
        form=None, query=None):
    request = {
        'method': method,
        'header': [{'key': 'Content-Type', 'value': 'application/json'}]
        if body is not None else [],
        'url': url(path),
        'description': desc,
    }
    if body is not None:
        request['body'] = {
            'mode': 'raw',
            'raw': json.dumps(body, indent=2),
            'options': {'raw': {'language': 'json'}},
        }
    if form is not None:
        request['header'] = []
        request['body'] = {'mode': 'formdata', 'formdata': form}
    if query:
        request['url']['query'] = query
        request['url']['raw'] += '?' + '&'.join(
            f'{q["key"]}={q["value"]}' for q in query
        )
    if not auth:
        request['auth'] = {'type': 'noauth'}

    item = {'name': name, 'request': request}
    if tests:
        item['event'] = [{
            'listen': 'test',
            'script': {'type': 'text/javascript', 'exec': tests},
        }]
    return item


ENVELOPE_TEST = [
    "const body = pm.response.json();",
    "pm.test('envelope reports success', function () {",
    "    pm.expect(body).to.have.property('code', 1);",
    "});",
]


def capture(var, path):
    return ENVELOPE_TEST + [
        "",
        f"const value = {path};",
        "if (value !== undefined && value !== null) {",
        f"    pm.collectionVariables.set('{var}', value);",
        f"    console.log('Stored {var} =', value);",
        "}",
    ]


# --- the collection --------------------------------------------------------

AUTH = [
    req('Register', 'POST', '/api/v1/users/register/', auth=False,
        body={'username': 'yul', 'email': 'yul@example.com',
              'password': 'correct-horse-battery', 'phone_number': '+639171234567'},
        desc=(
            'Create an account.\n\n'
            '`phone_number` is **optional but load-bearing for SMS**: a group '
            'texts its members on the number they verified, so an account '
            'without one can take part in the app and can never be reached by '
            'text.\n\n'
            'Returns `201` with the account and a `verification` block. A '
            'delivery failure still returns `201` with `verification: null` - '
            'the account is already committed at that point, and failing the '
            'request would strand the user with an account they cannot use.'
        )),
    req('Login', 'POST', '/api/v1/users/login/', auth=False,
        body={'username': 'yul', 'password': 'correct-horse-battery'},
        tests=ENVELOPE_TEST + [
            "",
            "const tokens = body.data && body.data.tokens;",
            "if (tokens) {",
            "    pm.collectionVariables.set('access_token', tokens.access);",
            "    pm.collectionVariables.set('refresh_token', tokens.refresh);",
            "}",
            "if (body.data && body.data.user) {",
            "    pm.collectionVariables.set('user_id', body.data.user.id);",
            "}",
        ],
        desc=(
            '**Run this first.** The test script stores the token pair and '
            '`user_id`, which everything else authenticates with.\n\n'
            '`username` accepts a username **or** an email address.\n\n'
            '`401` is deliberately identical for "no such user" and "wrong '
            'password", so this cannot be used to discover which accounts '
            'exist.'
        )),
    req('Verify email', 'POST', '/api/v1/users/verify-email/', auth=False,
        body={'email': 'yul@example.com', 'code': '{{otp_code}}'},
        tests=ENVELOPE_TEST + [
            "",
            "const tokens = body.data && body.data.tokens;",
            "if (tokens) {",
            "    pm.collectionVariables.set('access_token', tokens.access);",
            "    pm.collectionVariables.set('refresh_token', tokens.refresh);",
            "}",
        ],
        desc=(
            'Confirm the emailed code and receive tokens.\n\n'
            'This sets `Profile.email_verified`, **not** `user.is_active`. The '
            'two are kept apart on purpose: `is_active` means "an admin has '
            'not banned this account", and a verification code must never be '
            'able to write it - otherwise a banned user could change their '
            'email and verify their way back in.'
        )),
    req('Resend code', 'POST', '/api/v1/users/resend-code/', auth=False,
        body={'email': 'yul@example.com'},
        desc='Send a new email verification code. Rate limited per address and per account.'),
    req('Google sign-in', 'POST', '/api/v1/users/google/', auth=False,
        body={'id_token': '<google id token>'},
        desc=(
            'Sign in with a Google ID token, creating the account on first use.\n\n'
            'A Google account arrives with a verified email and **no phone '
            'number**, so it cannot receive group texts until one is added and '
            'verified under *Phone verification*.'
        )),
    req('Refresh token', 'POST', '/api/v1/users/token/refresh/', auth=False,
        body={'refresh': '{{refresh_token}}'},
        tests=capture('access_token', 'body.data && body.data.access'),
        desc='Exchange a refresh token for a new access token.'),
    req('Logout', 'POST', '/api/v1/users/logout/',
        body={'refresh': '{{refresh_token}}'},
        desc='Blacklist the refresh token. The access token stays valid until it expires.'),
]

ME = [
    req('Get my account', 'GET', '/api/v1/users/me/',
        desc='Username, email and verification state.'),
    req('Update my account', 'PATCH', '/api/v1/users/me/',
        body={'email': 'new@example.com'},
        desc=(
            'Changing the email address clears `email_verified` and sends a new '
            'code - otherwise anybody could move a verified badge onto an '
            'address they do not own.'
        )),
    req('Get my profile', 'GET', '/api/v1/users/me/profile/',
        desc=(
            'Display name, avatar, phone number and **`sms_enabled`** - the '
            'account-wide switch for every text this account can receive, '
            'across every group.'
        )),
    req('Update my profile', 'PATCH', '/api/v1/users/me/profile/',
        body={'display_name': 'Yul', 'bio': 'Manila', 'sms_enabled': True},
        desc=(
            'Note the two SMS switches and which one this is.\n\n'
            '| Switch | Where | Meaning |\n'
            '| --- | --- | --- |\n'
            '| `Profile.sms_enabled` | here | **all** texts, every group. A '
            'master off switch. |\n'
            '| `Participant.sms_enabled` | *Delivery settings* | whether **one '
            'group** texts you. |\n\n'
            'Both must be on for a text to go out. Setting `phone_number` here '
            'clears `phone_no_verified` - a number is only trusted once proved, '
            'and an unverified number is never texted.'
        )),
    req('Search people', 'GET', '/api/v1/users/search/',
        query=[{'key': 'q', 'value': 'bob'}],
        desc=(
            'Find somebody by username prefix, or by **exact** phone number.\n\n'
            'Phone matching is exact and requires the number to be **verified**. '
            'A prefix search would let anyone walk the directory a digit at a '
            'time, and an unverified match would hand back whoever merely typed '
            "someone else's number into their profile."
        )),
    req('Get a user by id', 'GET', '/api/v1/users/{{user_id}}/',
        desc='Public view of another account. Never includes email or phone number.'),
]

PHONE = [
    req('Request phone code', 'POST', '/api/v1/users/me/phone/request-code/',
        body={'phone_number': '+639171234567'},
        desc=(
            'Text a verification code.\n\n'
            '**This is what makes SMS work for you.** A group texts its members '
            'on the number they verified here; until this is done your account '
            'is app-only.\n\n'
            'Omit `phone_number` to re-send to the one already on the profile. '
            'Rate limited per account **and** per destination number - one '
            'account working through many numbers and many accounts hammering '
            'one number are different attacks, and counting only one misses '
            'the other.'
        )),
    req('Verify phone', 'POST', '/api/v1/users/me/phone/verify/',
        body={'code': '{{phone_otp_code}}'},
        desc=(
            'Confirm the code. Sets `phone_no_verified`, which is what every '
            'SMS path checks before texting you.'
        )),
]

CONVERSATIONS = [
    req('List my conversations', 'GET', '/api/v1/chat/conversations/',
        desc=(
            'Every conversation you are an **active** member of, newest '
            'activity first.\n\n'
            'Access control is the queryset itself: a conversation you are not '
            'in does not appear here and 404s everywhere else, so you cannot '
            'learn it exists. Members who left, were removed or are still '
            'pending are excluded.'
        )),
    req('Create group', 'POST', '/api/v1/chat/conversations/group/',
        body={'name': 'Family', 'description': 'Home things',
              'members': ['bob', '+639170000003']},
        tests=capture('conversation_id', 'body.data && body.data.id'),
        desc=(
            'Create a group. You become its admin.\n\n'
            '**The group is given its own phone number**, in the country of '
            'whoever created it, read from their verified number. That number '
            'never changes, and it is deleted when the group is.\n\n'
            'The number comes back as `sms_number` and may be `null` for a '
            'moment: buying it is a call to the telecom provider, made after '
            'the group is committed, so a slow provider cannot turn "create a '
            'group" into "create nothing".\n\n'
            '`members` takes usernames or **verified** `+E.164` numbers. Send '
            'an image by switching this request to form-data.'
        )),
    req('Get a conversation', 'GET', '/api/v1/chat/conversations/{{conversation_id}}/',
        desc=(
            'One conversation, its active members, its latest message and its '
            '`sms_number`.\n\n'
            '`sms_number` is read-only everywhere and for everybody. It is '
            'saved in members\' phones as the group\'s name, so a number that '
            'could be reassigned would deliver a stranger\'s messages.'
        )),
    req('Delete a group', 'DELETE', '/api/v1/chat/conversations/{{conversation_id}}/',
        desc=(
            'Delete a group. **Admins only, and it releases the phone number.**\n\n'
            'Returns `204`. A plain member gets `403`; somebody who is not in '
            'the group gets `404` and is not told it exists. One-to-one '
            'conversations cannot be deleted - they belong to two people '
            'equally and neither can end it for both.'
        )),
    req('Edit group settings', 'PATCH', '/api/v1/chat/conversations/{{conversation_id}}/settings/',
        body={'name': 'Family', 'description': 'Home things',
              'join_mode': 'code', 'mode': 'chat'},
        desc=(
            'Admins only. Every field optional.\n\n'
            '**`join_mode`** - how somebody who is not a member gets in:\n\n'
            '| Value | Meaning |\n'
            '| --- | --- |\n'
            '| `open` | anyone with the number or link joins immediately |\n'
            '| `code` | they must supply the group code |\n'
            '| `approval` | they ask; an admin approves or declines |\n'
            '| `invite` | nobody can ask; an admin adds them |\n\n'
            'New groups default to `invite`, the only mode that cannot surprise '
            'anybody. Switching to `code` generates a code if the group has none.\n\n'
            '**`mode`** - `chat` (everybody posts) or `broadcast` (only admins '
            'post; members still read). Enforced on every route at once, '
            'including inbound SMS.\n\n'
            'Editing settings deliberately does **not** bump the group up '
            "everyone's conversation list. Only a message is activity."
        )),
    req('Start direct chat', 'POST', '/api/v1/chat/conversations/direct/',
        body={'identifier': 'bob'},
        tests=capture('conversation_id', 'body.data && body.data.id'),
        desc=(
            'Open the one-to-one conversation with somebody, creating it only '
            'once. Safe to call every time a name is tapped: the same pair '
            'always resolves to the same conversation.\n\n'
            '`identifier` is a username or a **verified** `+E.164` number. '
            'Direct conversations have no phone number - SMS is a group feature.'
        )),
]

MEMBERSHIP = [
    req('Add members', 'POST', '/api/v1/chat/conversations/{{conversation_id}}/members/',
        body={'members': ['bob', '+639170000003']},
        desc=(
            'Admins only. Usernames or **verified** numbers of existing '
            'accounts.\n\n'
            'All or nothing: if one identifier cannot be resolved, or one '
            'person is blocked, nobody is added. To add somebody who has **no '
            'account**, use *Invite by phone* instead.'
        )),
    req('Invite by phone', 'POST', '/api/v1/chat/conversations/{{conversation_id}}/invite/',
        body={'phone_number': '+639171111111', 'display_name': 'Mom'},
        tests=capture('participant_id', 'body.data && body.data.id'),
        desc=(
            'Add somebody who has **no LoopD account**, by their number. '
            'Admins only. This is the way into an `invite` group.\n\n'
            'A name is required - they never chose one, and without it the '
            'group would see a masked number.\n\n'
            '**They are texted an invitation and receive nothing from the group '
            'until they reply `YES`.** Somebody else putting your number into a '
            'group is not you agreeing to be texted. The exact wording sent is '
            'stored on the row, because that is what a consent complaint turns '
            'on.'
        )),
    req('Join with a code', 'POST', '/api/v1/chat/conversations/join/',
        auth=True, body={'code': '{{join_code}}'},
        desc=(
            'Join a group using the code from its shareable link.\n\n'
            'Always by code, never by id - otherwise this endpoint would be a '
            'way to walk group ids and join every open group in the product.\n\n'
            'An unknown code, an invite-only group and a group you are blocked '
            'from all return the **same** refusal, byte for byte. Any difference '
            'would confirm that a guessed code is real.\n\n'
            'On an `approval` group the response is `{"status": "pending"}` and '
            'no conversation - you are not a member yet.'
        )),
    req('Get / rotate the join code', 'GET',
        '/api/v1/chat/conversations/{{conversation_id}}/join-code/',
        tests=capture('join_code', 'body.data && body.data.join_code'),
        desc=(
            '**Admins only.** Deliberately not on the conversation payload: the '
            'code is what lets a stranger in, so handing it to every member on '
            'every fetch would spread it through logs, caches and screenshots '
            'far beyond the people who administer the group.\n\n'
            '`POST` to the same URL issues a new code, which is how a leaked '
            'one is revoked.'
        )),
    req('Rotate the join code', 'POST',
        '/api/v1/chat/conversations/{{conversation_id}}/join-code/',
        tests=capture('join_code', 'body.data && body.data.join_code'),
        desc='Issue a new code. The old one stops working immediately.'),
    req('List pending requests', 'GET',
        '/api/v1/chat/conversations/{{conversation_id}}/requests/',
        desc=(
            'Admins only. Everybody waiting to be let into an `approval` group.\n\n'
            'Admins are also pushed a `conversation.request` event over the '
            'websocket as requests arrive, so the queue does not have to be '
            'polled.'
        )),
    req('Approve or decline', 'POST',
        '/api/v1/chat/conversations/{{conversation_id}}/requests/',
        body={'participant': '{{participant_id}}', 'decision': 'approve'},
        desc=(
            'Admins only. `decision` is `approve` or `decline`.\n\n'
            'Somebody with no account is **texted** either way, because they '
            'have no other way of finding out. Somebody with an account is not '
            '- they see it in the app, and texting them is a charge for '
            'something they already know.\n\n'
            'Approval re-checks identity across both a phone number and an '
            'account: a pending request can be the same person as a row blocked '
            'under their other identifier, and the queue does not show that.'
        )),
    req('Change a role', 'POST', '/api/v1/chat/conversations/{{conversation_id}}/role/',
        body={'participant': '{{participant_id}}', 'role': 'admin'},
        desc=(
            'Promote a member to admin, or demote them. Admins only.\n\n'
            '**This is also how a group\'s only admin gets out.** Leaving and '
            'being removed both refuse to take the last admin, because a group '
            'with none has no route back to having one - so hand over first, '
            'then leave.\n\n'
            'Somebody with no account cannot be an admin: every admin action is '
            'an authenticated request, so the role would be unusable while '
            'still counting towards that guard.'
        )),
    req('Rename a member', 'POST', '/api/v1/chat/conversations/{{conversation_id}}/rename/',
        body={'participant': '{{participant_id}}', 'display_name': 'Mom'},
        desc=(
            'Give a member with **no account** a name. Admins only.\n\n'
            'Somebody who joined by texting `JOIN` never gave one, so the group '
            'sees a masked number until an admin puts a name to it. A member '
            'who has an account keeps their own name - otherwise an admin could '
            'relabel another person in the group.'
        )),
    req('Remove or block', 'POST', '/api/v1/chat/conversations/{{conversation_id}}/remove/',
        body={'participant': '{{participant_id}}', 'block': False},
        desc=(
            'Admins only. `block: true` also stops them rejoining by **any** '
            'route - the join code, an admin re-adding them, texting `JOIN`, or '
            'registering an account on a blocked number.\n\n'
            'An admin cannot remove themselves (use *Leave*), and the last '
            'admin cannot be removed at all.\n\n'
            '**A block currently cannot be undone through the API.**'
        )),
    req('Leave', 'POST', '/api/v1/chat/conversations/{{conversation_id}}/leave/',
        desc=(
            'Take yourself out of a group. Your messages stay.\n\n'
            'Refused for the last admin - hand over with *Change a role* first. '
            'Refused on a one-to-one conversation, because there is no way back '
            'into one.'
        )),
    req('Delivery settings', 'POST',
        '/api/v1/chat/conversations/{{conversation_id}}/delivery/',
        body={'muted': False, 'sms_enabled': True},
        desc=(
            'How **this** conversation reaches **you**. Both fields optional; '
            'only what you send is written.\n\n'
            '| Field | Meaning |\n'
            '| --- | --- |\n'
            '| `muted` | silence this group on every channel. What texting '
            '`MUTE` sets. |\n'
            '| `sms_enabled` | whether texts are one of this group\'s channels '
            'for you. Off means app-only. |\n\n'
            'Both default **on** - you verified a number so your groups could '
            'reach you on it. These are yours alone; an admin cannot set them '
            'for somebody else.'
        )),
]

MESSAGES = [
    req('Get message history', 'GET',
        '/api/v1/chat/conversations/{{conversation_id}}/messages/',
        desc=(
            'Newest first, paginated.\n\n'
            'Each message carries a `source`: `app`, `sms` or `system`. There '
            'is **one message per message** however it arrived - a text sent '
            'from a handset and one typed in the app are the same row.\n\n'
            'A message sent by somebody with no account has `sender.id` and '
            '`sender.username` null, and their name in '
            '`sender.profile.display_label`. Render that field and both kinds '
            'work without branching.'
        )),
    req('Send a message', 'POST',
        '/api/v1/chat/conversations/{{conversation_id}}/messages/',
        body={'text': 'Dinner at 7'},
        desc=(
            'Post to the conversation. Text, attachments, or both - but not '
            'neither.\n\n'
            'The message goes to app members over the websocket immediately, '
            'and is **fanned out by SMS** to every member who should receive it '
            '- excluding you. Who that is depends on their delivery settings, '
            'their consent, and whether their number is verified; every '
            'decision not to send is recorded with a reason.\n\n'
            'In a `broadcast` group a non-admin gets `403`.'
        )),
    req('Send with attachments', 'POST',
        '/api/v1/chat/conversations/{{conversation_id}}/messages/',
        form=[
            {'key': 'text', 'value': 'Look at this', 'type': 'text'},
            {'key': 'attachments', 'type': 'file', 'src': []},
        ],
        desc=(
            'Multipart. Repeat `attachments` for several files.\n\n'
            'SMS members cannot receive a photo, so they get a line telling '
            'them to open the app rather than an empty message.'
        )),
]

WEBHOOK = [
    req('Inbound SMS (provider callback)', 'POST', '/api/v1/sms/webhooks/inbound/',
        auth=False,
        body={'data': {'id': 'evt_1', 'event_type': 'message.received',
                       'payload': {'from': {'phone_number': '+639171111111'},
                                   'to': [{'phone_number': '+14155551001'}],
                                   'text': 'JOIN'}}},
        desc=(
            '**Called by the telecom provider, not by a client.** Included so '
            'the inbound path can be exercised without a real phone.\n\n'
            'Authenticated by an Ed25519 signature over the raw body, not by a '
            'token. With `TELNYX_PUBLIC_KEYS` unset (development) the signature '
            'is not checked and this request works as written.\n\n'
            '### Status codes are the contract\n'
            'A provider reads any non-2xx as "resend this", so:\n\n'
            '| Code | When |\n'
            '| --- | --- |\n'
            '| `200` | anything we understand and will not change our mind '
            'about - duplicate, unknown number, unknown sender, malformed |\n'
            '| `401` | bad signature |\n'
            '| `500` | we failed to handle it - **please do resend** |\n\n'
            '### Commands\n'
            '`JOIN [code]`, `HELP`, `MUTE`, `UNMUTE`, `LEAVE`, `STOP`, `YES`. '
            'A command must be the **whole message** - "stop by the shop" is a '
            'message to your family, not an opt-out.\n\n'
            'Repeat with the same `data.id` to see de-duplication: the second '
            'call returns `duplicate: true` and posts nothing.'
        )),
]

COLLECTION = {
    'info': {
        'name': "Loopd'n API v2",
        'description': (
            "# Loopd'n API v2\n\n"
            'Every reply is wrapped in the same envelope:\n\n'
            '```json\n{ "code": 1, "message": "...", "data": { } }\n```\n\n'
            '`code` is `1` for success and `-1` for failure; the HTTP status '
            'stays meaningful. `data` is the payload, or the field errors on a '
            'failure.\n\n'
            '## Start here\n'
            '1. **Register**, then **Verify email** (the code is in the server '
            'log when `DEBUG` is on).\n'
            '2. **Login** - stores your tokens automatically.\n'
            '3. **Request phone code** then **Verify phone** - without a '
            'verified number your account can never receive a group text.\n'
            '4. **Create group** - stores `conversation_id` and gives the group '
            'its own phone number.\n\n'
            '## What is new since v1\n'
            '- **Every group has its own phone number.** Bought on creation in '
            "the creator's country, never changes, deleted with the group.\n"
            '- **Membership has states** - active, pending, left, removed, '
            'blocked - and four join modes.\n'
            '- **Roles**: promote and demote admins.\n'
            '- **Broadcast groups** where only admins post.\n'
            '- **Members without accounts**, reachable only by SMS.\n'
            '- **Delivery settings** per group, and account-wide.\n'
            '- **The inbound SMS webhook.**\n\n'
            '## Two things that surprise people\n'
            '**404 means "not yours".** Access control is the database query, '
            'not a check bolted on top - so a conversation you are not in is '
            'indistinguishable from one that does not exist. That is '
            'deliberate.\n\n'
            '**Two SMS switches.** `Profile.sms_enabled` (account-wide) and '
            '`Participant.sms_enabled` (this group). Both must be on, plus a '
            'verified number, plus consent for members with no account.'
        ),
        'schema': 'https://schema.getpostman.com/json/collection/v2.1.0/collection.json',
    },
    'auth': {'type': 'bearer',
             'bearer': [{'key': 'token', 'value': '{{access_token}}', 'type': 'string'}]},
    'variable': [
        {'key': 'base_url', 'value': 'http://127.0.0.1:8000'},
        {'key': 'access_token', 'value': ''},
        {'key': 'refresh_token', 'value': ''},
        {'key': 'user_id', 'value': ''},
        {'key': 'conversation_id', 'value': ''},
        {'key': 'participant_id', 'value': ''},
        {'key': 'join_code', 'value': ''},
        {'key': 'otp_code', 'value': ''},
        {'key': 'phone_otp_code', 'value': ''},
    ],
    'item': [
        {'name': '1. Auth & accounts', 'item': AUTH},
        {'name': '2. Me & profile', 'item': ME},
        {'name': '3. Phone verification', 'item': PHONE},
        {'name': '4. Conversations', 'item': CONVERSATIONS},
        {'name': '5. Membership & joining', 'item': MEMBERSHIP},
        {'name': '6. Messages', 'item': MESSAGES},
        {'name': '7. SMS webhook (provider)', 'item': WEBHOOK},
    ],
}

if __name__ == '__main__':
    OUT.write_text(json.dumps(COLLECTION, indent=2) + '\n')
    count = sum(len(group['item']) for group in COLLECTION['item'])
    print(f'Wrote {OUT} ({count} requests, {OUT.stat().st_size // 1024}KB)')
