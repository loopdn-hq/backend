"""Generate the v4 Postman collection - v3 plus the billing endpoints.

Run:  python postman/build_v4.py
"""
import json
import pathlib

BASE = '{{base_url}}'
OUT = pathlib.Path(__file__).parent / 'loopd_v4.postman_collection.json'


def url(path):
    """A Postman url object from '/api/v1/...' with {{vars}} left intact.

    The TRAILING SLASH is preserved, and that is not cosmetic. Django routes
    every endpoint here with one, and `APPEND_SLASH` turns a slash-less request
    into a 301 to the slash version - which a client re-issues as GET, silently
    dropping the body of every POST and PATCH. Postman represents a trailing
    slash as a final empty path segment.
    """
    clean = path.strip('/')
    segments = clean.split('/')
    if path.endswith('/'):
        clean += '/'
        segments = segments + ['']
    return {
        'raw': f'{BASE}/{clean}',
        'host': [BASE],
        'path': segments,
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
              'password': 'correct-horse-battery',
              'confirm_password': 'correct-horse-battery',
              'phone_number': '+639171234567'},
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
            '**Register and verify your email first** - with email verification on, '
            'this returns 401 until the account is verified, and *Verify email* '
            'already hands back a token pair of its own.\n\n'
            'The test script stores the token pair and `user_id`, which '
            'everything else authenticates with.\n\n'
            '`username` accepts a username **or** an email address.\n\n'
            '`401` is deliberately identical for "no such user" and "wrong '
            'password", so this cannot be used to discover which accounts '
            'exist.'
        )),
    req('Resend code', 'POST', '/api/v1/users/resend-code/', auth=False,
        body={'email': 'yul@example.com'},
        desc='Send a new email verification code. Rate limited per address and per account.'),
    req('Google sign-in', 'POST', '/api/v1/users/google/', auth=False,
        body={'credential': '<google id token>'},
        desc=(
            'Sign in with a Google ID token, creating the account on first use.\n\n'
            'A Google account arrives with a verified email and **no phone '
            'number**, so it cannot receive group texts until one is added and '
            'verified under *Phone verification*.'
        )),
    req('Refresh token', 'POST', '/api/v1/users/token/refresh/', auth=False,
        body={'refresh': '{{refresh_token}}'},
        tests=ENVELOPE_TEST + [
            "",
            "const tokens = body.data && body.data.tokens;",
            "if (tokens) {",
            "    pm.collectionVariables.set('access_token', tokens.access);",
            "    pm.collectionVariables.set('refresh_token', tokens.refresh);",
            "}",
        ],
        desc='Exchange a refresh token for a new access token.'),
    req('Forgot password', 'POST', '/api/v1/users/forgot-password/', auth=False,
        body={'email': 'yul@example.com'},
        desc=(
            'Email a password-reset link.\n\n'
            'Always answers the same way whether or not the address exists - '
            'a different reply for an unknown address would turn this into a '
            'way to discover who has an account.'
        )),
    req('Reset password', 'POST', '/api/v1/users/reset-password/', auth=False,
        body={'uid': '<from the emailed link>', 'token': '<from the emailed link>',
              'new_password': 'a-different-passphrase',
              'confirm_password': 'a-different-passphrase'},
        desc=(
            'Finish the reset with the `uid` and `token` out of the emailed '
            'link.\n\nBoth are single-use: the token stops working once the '
            'password changes, so a leaked link cannot be replayed.'
        )),
    req('Change password', 'POST', '/api/v1/users/change-password/',
        body={'old_password': 'correct-horse-battery',
              'new_password': 'a-different-passphrase'},
        desc=(
            'Change your own password while signed in. `old_password` is '
            'required - an access token alone must not be enough to lock the '
            'real owner out.'
        )),
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
            'Display name, avatar, phone number, and whether the number and '
            'email are verified.\n\n'
            '**`sms_enabled` is not in this payload**, despite being a real '
            'column on the profile. It is on no serializer, so a client reading '
            '`data.sms_enabled` gets `undefined` rather than the switch it '
            'expects - see *Update my profile* for what actually sets it.'
        )),
    req('Update my profile', 'PATCH', '/api/v1/users/me/profile/',
        body={'display_name': 'Yul', 'bio': 'Manila'},
        desc=(
            'Setting `phone_number` here clears `phone_no_verified` - a number '
            'is only trusted once proved, and an unverified number is never '
            'texted. Send `avatar` as form-data to set a photo.\n\n'
            '**The two SMS switches, and why neither is set here:**\n\n'
            '| Switch | Set by | Meaning |\n'
            '| --- | --- | --- |\n'
            '| `Profile.sms_enabled` | **texting STOP** - no endpoint | **all** '
            'texts, every group. A master off switch. |\n'
            '| `Participant.sms_enabled` | *Delivery settings* | whether **one '
            'group** texts you. |\n\n'
            'Both must be on for a text to go out.\n\n'
            '`sms_enabled` is **not a field on this endpoint**, and sending it '
            'here does nothing: DRF drops keys a serializer does not declare, '
            'so the request returns 200 and the switch is unchanged. The '
            'account-wide switch is currently only reachable by replying STOP '
            'to a message - worth knowing before building a settings toggle '
            'against it.'
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
            'The number comes back as `sms_number`, an object of `number`, '
            '`country`, `status` and `provider`. The whole object may be '
            '`null` for a moment: buying it is a call to the telecom '
            'provider, made after the group is committed, so a slow provider '
            'cannot turn "create a group" into "create nothing".\n\n'
            'An **empty `provider`** means no provider is configured and the '
            'number is a placeholder - well-formed, but it belongs to nobody '
            'and texting it does nothing. Do not tell a user to text a number '
            'whose `provider` is empty.\n\n'
            '`members` takes usernames or **verified** `+E.164` numbers. Send '
            'an image by switching this request to form-data.\n\n'
            '**Plan limits apply.** Creating a group beyond your plan\'s '
            '`max_groups` (see *9. Billing*) returns `403`.'
        )),
    req('Get a conversation', 'GET', '/api/v1/chat/conversations/{{conversation_id}}/',
        desc=(
            'One conversation: its settings, its latest message, its '
            '`sms_number`, and who runs it.\n\n'
            '| Field | |\n'
            '| --- | --- |\n'
            '| `created_by` | who started it, as `{username, avatar}` |\n'
            '| `is_admin` | whether **you** administer it - the creator counts '
            'automatically, anyone else needs the admin role |\n'
            '| `admin_details` | who administers it, as `{username, avatar}`. '
            'A group can have several admins and this is one object, so it '
            'prefers the creator, then the earliest to join |\n\n'
            '`is_admin` and `admin_details` are group concepts: a one-to-one '
            'gets `false` and `null`.\n\n'
            '**No `participants`.** The member list is unbounded and every row '
            'nests a user and a profile, so a group of two hundred would put '
            'two hundred profiles in every fetch - and once per conversation '
            'on the list endpoint. Use *List members* instead.\n\n'
            '`sms_number` is read-only everywhere and for everybody. It is '
            'saved in members\' phones as the group\'s name, so a number that '
            'could be reassigned would deliver a stranger\'s messages.\n\n'
            'Fields: `number`, `country`, `status`, `provider`. An empty '
            '`provider` marks a placeholder that cannot actually receive text.'
        )),
    req('Edit group settings', 'PATCH', '/api/v1/chat/conversations/{{conversation_id}}/settings/',
        body={'name': 'Family', 'description': 'Home things',
              'settings': {'mode': 'chat', 'join_mode': 'code',
                           'add_member_permission': 'admins',
                           'edit_group_permission': 'admins',
                           'sms_delivery_enabled': True,
                           'sms_replies_enabled': True,
                           'voice_messages_enabled': True}},
        desc=(
            'Admins only. Every field optional.\n\n'
            'The seven settings go **inside `settings`**, the same object '
            '`POST /group/` takes - there is one shape to learn. Sending them '
            'at the top level is silently ignored: DRF drops fields a '
            'serializer does not declare, so the request returns 200 and '
            'changes nothing.\n\n'
            'All seven come back under `settings` on the conversation object, '
            'the same shape you sent - so a settings screen renders from one '
            'fetch and there is one shape to learn. `join_code` does not: it '
            'has its own admin-only endpoint.\n\n'
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
            "everyone's conversation list. Only a message is activity.\n\n"
            'The reply is the conversation - which carries no `participants` '
            'on any endpoint. Use *List members* for the member list.'
        )),
    req('Start direct chat', 'POST', '/api/v1/chat/conversations/direct/',
        body={'identifier': 'bob'},
        # Its OWN variable. Capturing into `conversation_id` would replace the
        # group every later request needs, and every group-only endpoint would
        # then answer 'Only groups can...' against a one-to-one thread.
        tests=capture('direct_conversation_id', 'body.data && body.data.id'),
        desc=(
            'Open the one-to-one conversation with somebody, creating it only '
            'once. Safe to call every time a name is tapped: the same pair '
            'always resolves to the same conversation.\n\n'
            '`identifier` is a username or a **verified** `+E.164` number. '
            'Direct conversations have no phone number - SMS is a group feature.'
        )),
]

MEMBERSHIP = [
    req('List members', 'GET', '/api/v1/chat/conversations/{{conversation_id}}/participants/',
        query=[{'key': 'page', 'value': '1'},
               {'key': 'page_size', 'value': '10'}],
        # Stores the second row - an account member - as `member_participant_id`.
        # `participant_id` comes from *Invite by phone* and is somebody with no
        # account, who cannot be made an admin, so role changes need this one.
        tests=capture('member_participant_id', 'body.data && body.data.results[1].id'),
        desc=(
            'Who is in this conversation, **ten at a time**.\n\n'
            'Its own endpoint rather than a field on every conversation reply: '
            'the member list is unbounded and each row carries a nested user '
            'and profile, so a group of two hundred made every fetch of the '
            'group carry two hundred profiles.\n\n'
            'Returns the standard page envelope inside `data`:\n\n'
            '```\n'
            '{ "count": 17, "next": "...?page=2", "previous": null,\n'
            '  "results": [ ...participants... ] }\n'
            '```\n\n'
            '`page_size` raises the page up to a maximum of 100. **Active '
            'members only** - somebody who left, was removed or is waiting on '
            'approval is not a member; pending requests have their own '
            'endpoint, *Join requests*.\n\n'
            'A non-member gets **404**, not 403 - access control here is the '
            'queryset, so an outsider cannot learn the conversation exists.'
        )),
    req('Add members', 'POST', '/api/v1/chat/conversations/{{conversation_id}}/members/',
        body={'members': ['bob', '+639170000003']},
        desc=(
            'Usernames or **verified** numbers of existing accounts.\n\n'
            '**Admins only by default, but not always.** The gate is '
            '`may_add_members`, so in a group whose '
            '`add_member_permission` is `members`, any active member may '
            'add people.\n\n'
            'All or nothing: if one identifier cannot be resolved, or one '
            'person is blocked, nobody is added. To add somebody who has **no '
            'account**, use *Invite by phone* instead.\n\n'
            '**Plan limits apply.** Adding people past the group owner\'s plan '
            '`max_members_per_group` (see *9. Billing*) returns `403`.'
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
    req('Change a role', 'POST', '/api/v1/chat/conversations/{{conversation_id}}/role/',
        body={'participant': '{{member_participant_id}}', 'role': 'admin'},
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
            '`sms_enabled` defaults **on** and `muted` defaults **off** - you verified a number so your groups could '
            'reach you on it. These are yours alone; an admin cannot set them '
            'for somebody else.'
        )),
]

MESSAGES = [
    req('Get message history', 'GET',
        '/api/v1/chat/conversations/{{conversation_id}}/messages/',
        desc=(
            'Newest first, paginated.\n\n'
            '**`?after_id=<id>` resumes.** Returns only messages newer than '
            'that id, **oldest first**, so a client that reconnects can fetch '
            'exactly what it missed and append it in order. Without it a '
            'client can only re-page the newest-first history and diff, which '
            'is unbounded and gets worse the longer it was disconnected.\n\n'
            'Keyed on `id` rather than `created_at` deliberately: created_at '
            'is app-server wall clock, so two tasks can stamp messages out of '
            'order, and it has no tiebreak. Ids come from one sequence.\n\n'
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
        body={'text': 'Dinner at 7', 'client_id': '{{$guid}}'},
        desc=(
            '**`client_id` makes a retry safe.** It is your own id for this '
            'message. A send that times out - or a websocket that drops '
            'mid-send - leaves you unable to tell whether it landed; retrying '
            'with the SAME `client_id` returns the message already stored '
            'instead of posting a second one. Without it, a retry creates a '
            'second row with a different id that no client-side dedupe can '
            'collapse, and schedules a **second billed SMS fan-out to every '
            "member's phone**.\n\n"
            'Optional, scoped per conversation, and echoed back on the message '
            'so you can match it to the entry you rendered optimistically. '
            '`{{$guid}}` is a Postman variable that generates a fresh one per '
            'send.\n\n'
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
            '| `403` | bad signature - DRF answers 403, not 401, because the '
            'authentication class returns no WWW-Authenticate header |\n'
            '| `500` | we failed to handle it - **please do resend** |\n\n'
            '### Commands\n'
            '`JOIN [code]`, `HELP`, `MUTE`, `UNMUTE`, `LEAVE`, `STOP`, `YES`. '
            'A command must be the **whole message** - "stop by the shop" is a '
            'message to your family, not an opt-out.\n\n'
            'Repeat with the same `data.id` to see de-duplication: the second '
            'call returns `duplicate: true` and posts nothing.'
        )),
]

BILLING = [
    req('List plans', 'GET', '/api/v1/billing/plans/', auth=False,
        desc=(
            'The pricing page: Free, Premium, Business, Enterprise - their '
            'monthly price, group/member/admin limits, storage, and whether '
            'WhatsApp/Telegram are included.\n\n'
            'Public, so a logged-out visitor sees pricing before signing up. '
            '`price_display` is `"Custom"` for Enterprise, which is quoted by '
            'sales rather than charged through Stripe - `monthly_price_cents` '
            'is `null` for the same reason.'
        )),
    req('List credit packages', 'GET', '/api/v1/billing/credit-packages/', auth=False,
        tests=capture('credit_package_id', 'body.data && body.data[0] && body.data[0].id'),
        desc=(
            'The Pay-As-You-Go Communication Credit top-ups: Starter ($5) '
            'through High Volume ($100). `credit_cents` is what lands in the '
            'wallet - today it matches `price_cents` exactly, but the two are '
            'separate fields so a future bonus package (pay $10, get $12 of '
            'credit) is just a row where they differ.\n\n'
            'Public, for the same reason *List plans* is.'
        )),
    req('Billing summary', 'GET', '/api/v1/billing/summary/',
        desc=(
            'What the caller is on right now: their `subscription` (plan, '
            'status, renewal date) and their `wallet` (balance, auto-recharge).\n\n'
            'Every user has both from the moment they sign up - a fresh '
            'account is on the Free plan with a zero-balance wallet, not a '
            'missing row, so a client never has to special-case "no '
            'subscription yet".'
        )),
    req('Billing analytics', 'GET', '/api/v1/billing/analytics/',
        desc=(
            'Analytics access is feature-gated by the caller\'s current plan.\n\n'
            'If the plan has **Basic Analytics** (or **Advanced Analytics**), '
            'the response includes the `basic` section: conversation counts '
            'and 30-day message activity.\n\n'
            'If the plan has **Advanced Analytics**, `advanced` is included '
            'with group-admin and group-size signals. Otherwise `advanced` is '
            '`null`.\n\n'
            'The endpoint also returns `suggestions`, tailored by access '
            'level (`none`, `basic`, `advanced`) so clients can guide upgrades '
            'or improvements.'
        )),
    req('Checkout: subscribe to a plan', 'POST', '/api/v1/billing/checkout/plan/',
        body={'plan_code': 'premium',
              'success_url': 'https://loopdn.com/billing/success',
              'cancel_url': 'https://loopdn.com/billing/cancel'},
        desc=(
            'Start a Stripe Checkout Session that subscribes the caller to '
            'Premium or Business on completion. Redirect the browser/webview '
            'to the returned `checkout_url`; Stripe redirects back to '
            '`success_url` or `cancel_url` when the customer is done.\n\n'
            'Free needs no checkout, and Enterprise is quoted by sales - both '
            'return `400` here. A plan whose Stripe price has not been synced '
            'yet (`manage.py sync_stripe_billing`) also returns `400`.\n\n'
            'The actual upgrade happens off the '
            '`checkout.session.completed` webhook, not this response - '
            'the session can be abandoned, so nothing changes until Stripe '
            'confirms it was paid.'
        )),
    req('Checkout: buy communication credits', 'POST', '/api/v1/billing/checkout/credits/',
        body={'package_id': '{{credit_package_id}}',
              'success_url': 'https://loopdn.com/billing/success',
              'cancel_url': 'https://loopdn.com/billing/cancel'},
        desc=(
            'Start a Stripe Checkout Session (one-time payment, not a '
            'subscription) that tops up the caller\'s Communication Wallet on '
            'completion. Same redirect shape as *Checkout: subscribe to a '
            'plan*.\n\n'
            'The wallet is credited off the webhook once Stripe confirms '
            'payment, with a ledger row recorded against it - see *Billing '
            'summary* for the resulting balance.'
        )),
    req('Billing portal', 'POST', '/api/v1/billing/portal/',
        body={'return_url': 'https://loopdn.com/settings/billing'},
        desc=(
            'A link to Stripe\'s own hosted page for updating a card, viewing '
            'invoices, or cancelling a subscription. Redirect to '
            '`portal_url`; Stripe sends the customer back to `return_url` '
            'when they are done.\n\n'
            'Nothing here is stored locally - Stripe is the source of truth '
            'for payment methods, and the webhook is what brings a '
            'cancellation back into `Subscription.status`.'
        )),
    req('Stripe webhook (provider callback)', 'POST', '/api/v1/billing/webhooks/stripe/',
        auth=False,
        body={'id': 'evt_1', 'type': 'checkout.session.completed',
              'data': {'object': {'id': 'cs_1', 'mode': 'payment',
                                  'customer': 'cus_1', 'payment_intent': 'pi_1',
                                  'client_reference_id': '{{user_id}}',
                                  'metadata': {'credit_package_id': '{{credit_package_id}}'}}}},
        desc=(
            '**Called by Stripe, not by a client.** Included so the shape of '
            'every event this project understands is documented in one '
            'place: `checkout.session.completed` (subscription activation or '
            'a credit top-up), `customer.subscription.updated` and '
            '`customer.subscription.deleted`.\n\n'
            'Authenticated by a signature over the raw body '
            '(`Stripe-Signature` header, verified against '
            '`STRIPE_WEBHOOK_SECRET`), not a token - like the SMS webhook, '
            'this endpoint has no other way to prove Stripe sent it. Running '
            'this request as written returns `400`: Postman cannot mint a '
            'valid signature, only the Stripe CLI (`stripe trigger '
            '...`) or a real event can.\n\n'
            '### Status codes are the contract\n'
            '| Code | When |\n'
            '| --- | --- |\n'
            '| `200` | understood and handled, including a duplicate event |\n'
            '| `400` | bad signature or unparseable body - never retried |\n'
            '| `500` | a known event failed to process - Stripe retries |\n\n'
            'Repeat with the same event `id` to see de-duplication: the '
            'second call returns `duplicate: true` and changes nothing.'
        )),
]

CLEANUP = [
    req('Delete a group', 'DELETE', '/api/v1/chat/conversations/{{conversation_id}}/',
        desc=(
            'Delete a group. **Admins only, and it releases the phone number.**\n\n'
            'Returns `204`. A plain member gets `403`; somebody who is not in '
            'the group gets `404` and is not told it exists. One-to-one '
            'conversations cannot be deleted - they belong to two people '
            'equally and neither can end it for both.'
        )),
]

COLLECTION = {
    'info': {
        'name': "Loopd'n API v4",
        'description': (
            "# Loopd'n API v4\n\n"
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
            '## What is new since v3\n'
            '- **`9. Billing`** - the four platform plans (Free, Premium, '
            'Business, Enterprise), Pay-As-You-Go Communication Credit '
            'packages, a billing summary, Stripe Checkout for both a plan '
            'subscription and a credit top-up, the Stripe billing portal, and '
            'the Stripe webhook that keeps `Subscription` and the wallet in '
            'sync.\n'
            '- **Plan limits are enforced.** *Create group* and *Add members* '
            'now return `403` once a Free/Premium/Business plan\'s '
            '`max_groups` or `max_members_per_group` would be exceeded - see '
            '*List plans*.\n\n'
            '## What was new in v3\n'
            '- **Every URL now ends in a slash.** v1 and v2 stripped it, and '
            'Django answers a slash-less POST with a 301 that drops the body - '
            'so every write request in them silently did nothing.\n'
            '- **Payloads fixed**: `Register` and `Reset password` were missing '
            '`confirm_password`; `Google sign-in` sent `id_token` where the '
            'serializer wants `credential`; `Update my profile` sent an '
            '`sms_enabled` that no serializer declares.\n'
            '- **Runs top to bottom.** Verify email now precedes Login, the '
            'join code is fetched before it is used, and *Delete a group* moved '
            'to its own final folder instead of destroying the group halfway '
            'through.\n'
            '- **Forgot / reset / change password** were missing entirely.\n'
            '- **`GET .../participants/`** - the member list, paginated ten at '
            'a time. Conversation payloads no longer embed it.\n\n'
            '## What was new in v2\n'
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
        {'key': 'direct_conversation_id', 'value': ''},
        {'key': 'participant_id', 'value': ''},
        {'key': 'member_participant_id', 'value': ''},
        {'key': 'join_code', 'value': ''},
        {'key': 'otp_code', 'value': ''},
        {'key': 'phone_otp_code', 'value': ''},
        {'key': 'credit_package_id', 'value': ''},
    ],
    'item': [
        {'name': '1. Auth & accounts', 'item': AUTH},
        {'name': '2. Me & profile', 'item': ME},
        {'name': '3. Phone verification', 'item': PHONE},
        {'name': '4. Conversations', 'item': CONVERSATIONS},
        {'name': '5. Membership & joining', 'item': MEMBERSHIP},
        {'name': '6. Messages', 'item': MESSAGES},
        {'name': '7. SMS webhook (provider)', 'item': WEBHOOK},
        {'name': '8. Billing', 'item': BILLING},
        {'name': '9. Cleanup (destructive)', 'item': CLEANUP},
    ],
}

if __name__ == '__main__':
    OUT.write_text(json.dumps(COLLECTION, indent=2) + '\n')
    count = sum(len(group['item']) for group in COLLECTION['item'])
    print(f'Wrote {OUT} ({count} requests, {OUT.stat().st_size // 1024}KB)')
