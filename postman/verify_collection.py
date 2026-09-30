"""Execute every request in a Postman collection and report what happened.

    python postman/verify_collection.py                    # newest collection
    python postman/verify_collection.py loopd_v3.postman_collection.json

WHY THIS EXISTS
---------------
A collection is documentation that goes stale silently. A path that no longer
exists 404s; a body key the serializer stopped declaring is DROPPED by DRF and
the request still returns 200 while changing nothing. Neither shows up by
reading the file, and both have already happened in this repo.

So the collection is not "written carefully" - it is run. Every request is
driven through Django's test client against a throwaway database, in the order
a person would click them, with `{{variables}}` substituted from the same
capture rules the collection carries in its own test scripts.

WHAT A FAILURE MEANS
--------------------
    404  the path is wrong, or the fixture the request needs was never created
    400  the body does not match the serializer - the interesting case
    401  authentication was expected and the token was not captured
    405  the method is wrong for that action

Some non-2xx results are CORRECT and expected: the SMS webhook must 401 without
a signature, and requests that deliberately demonstrate a failure should fail.
Those are listed in EXPECTED below, with a reason, so that an unexplained
non-2xx is always a real finding.

This is deliberately NOT a test in any app's tests.py: it needs the built
collection JSON, which is a generated artifact, and it is a tool you run when
you change the API rather than a gate on every commit.
"""
import json
import os
import re
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import django

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'loopd.settings')
django.setup()

from django.test import override_settings  # noqa: E402
from django.test.utils import setup_test_environment, teardown_test_environment  # noqa: E402
from django.test.runner import DiscoverRunner  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402


#: Requests whose non-2xx result is the correct answer, and why. Anything else
#: that fails is a real problem with the collection or the API.
EXPECTED = {
    'Resend code': (
        400, 'the account verified a moment ago, so there is nothing to resend'),
    'Google sign-in': (
        400, 'needs a real Google credential, which cannot be minted here'),
    'Leave': (
        400, "the signed-in user is the group's only admin, and the last admin "
             'cannot leave - this failure IS the documented behaviour'),
    'Reset password': (
        400, 'uid and token come out of a real email link; there is nothing '
             'valid to substitute in an unattended run'),
    'Approve or decline': (
        400, 'nobody is waiting for approval in a linear run: the only '
             'participant to hand is the one just invited, who is already active'),
    'Checkout: subscribe to a plan': (
        400, 'no Stripe price is configured for the plan until '
             '`manage.py sync_stripe_billing` has run against a real Stripe account'),
    'Checkout: buy communication credits': (
        400, 'no Stripe price is configured for the credit package until '
             '`manage.py sync_stripe_billing` has run against a real Stripe account'),
    'Billing portal': (
        502, 'STRIPE_SECRET_KEY is unset in this environment, so Stripe '
             'rejects the API call'),
    'Stripe webhook (provider callback)': (
        400, 'Postman cannot mint a valid Stripe signature; only the Stripe '
             'CLI or a real event can'),
    'Stripe webhook: plan catalog sync (provider callback)': (
        400, 'same reason as the request above - no valid signature'),
    'Decline a call': (
        400, 'the signed-in user started the call, and cannot decline their '
             'own - somebody else has to'),
    'LiveKit webhook (provider callback)': (
        400, 'Postman cannot mint a valid LiveKit signature; only LiveKit '
             'itself can'),
}

#: Requests to skip, with the reason. Kept short on purpose - a skip is a hole
#: in the proof, so each one has to earn its place.
SKIP = {
    'Refresh token': 'consumes the refresh token, breaking every later request',
    'Send with attachments':
        'needs a real file on disk; the runner sends form fields, not uploads',
}

VAR = re.compile(r'\{\{(\w+)\}\}')
CONST = re.compile(r'const (\w+) = ([^;]+);')
SET_CALL = re.compile(r"pm\.collectionVariables\.set\('(\w+)',\s*([^)]+)\)")


def substitute(text, variables):
    return VAR.sub(lambda m: str(variables.get(m.group(1), m.group(0))), text)


def lookup(expr, scope):
    """Resolve `a.b.c`, or the longest term of a `x && x.y` guard, in a scope.

    Deliberately not a JavaScript interpreter. The collection's capture scripts
    are all guards and dotted lookups; if one ever needs real logic, the
    collection is doing too much and should be simplified rather than this
    being made cleverer.
    """
    longest = max((t.strip() for t in expr.split('&&')), key=len)
    value = scope
    for part in longest.split('.'):
        part = part.strip()
        # `results[1]` - one level of indexing, which is all a capture needs to
        # reach a specific row of a paginated list.
        index = None
        if part.endswith(']') and '[' in part:
            part, raw_index = part[:-1].split('[', 1)
            index = int(raw_index)
        if not isinstance(value, dict):
            return None
        value = value.get(part)
        if index is not None:
            if not isinstance(value, list) or len(value) <= index:
                return None
            value = value[index]
    return value


def capture_from(item, body, variables):
    """Apply the request's own test script, so run order matches Postman's.

    Two shapes appear: the `capture()` helper's `const value = ...` plus a set,
    and hand-written scripts that bind an intermediate (`const tokens = ...`)
    and set several variables off it. Binding consts into a scope first handles
    both without special-casing either.
    """
    for event in item.get('event') or []:
        script = '\n'.join(event.get('script', {}).get('exec') or [])
        scope = {'body': body}
        for name, expr in CONST.findall(script):
            # Only bind what resolves. The scripts open with
            # `const body = pm.response.json();`, which means nothing here -
            # binding its None would clobber the real response body and every
            # later lookup would silently return None.
            value = lookup(expr, scope)
            if value is not None:
                scope[name] = value
        for name, expr in SET_CALL.findall(script):
            value = lookup(expr, scope)
            if value is not None:
                variables[name] = value


def livekit_stack():
    """LiveKit and S3, stood in for.

    The Calls folder is exercised for real - real tokens, real rows, real
    permission and plan checks - with only what would leave the process
    replaced: LiveKit's egress and room APIs, and the recordings bucket. Without
    this every call request would answer 503, which proves nothing.
    """
    from livekit import api as lk

    stack = ExitStack()
    stack.enter_context(override_settings(
        LIVEKIT_URL='wss://livekit.example.test',
        LIVEKIT_API_KEY='verify-key',
        LIVEKIT_API_SECRET='verify-secret-verify-secret-verify-secret',
        LIVEKIT_RECORDING_BUCKET='verify-bucket',
    ))
    stack.enter_context(mock.patch(
        'calls.livekit.start_recording',
        return_value=lk.EgressInfo(
            egress_id='EG_verify', status=lk.EgressStatus.EGRESS_ACTIVE),
    ))
    stack.enter_context(mock.patch(
        'calls.livekit.stop_recording',
        return_value=lk.EgressInfo(
            egress_id='EG_verify', status=lk.EgressStatus.EGRESS_ENDING),
    ))
    stack.enter_context(mock.patch('calls.livekit.delete_room'))
    stack.enter_context(mock.patch(
        'calls.storage.download_url', return_value='https://signed.example/recording.mp4'))
    stack.enter_context(mock.patch('calls.storage.delete_file'))
    return stack


def whatsapp_stack():
    """Meta's Graph API, stood in for.

    Embedded Signup cannot be completed without a real business on the other
    side, so the onboarding calls answer as Meta would for the collection's own
    `whatsapp_waba_id` and `whatsapp_phone_number_id`. Sending needs no stub:
    with no WHATSAPP_APP_ID it logs instead of calling Meta, which is exactly
    the local-development behaviour worth proving. The webhook stays unsigned
    for the same reason - no app secret is set here.
    """
    stack = ExitStack()
    stack.enter_context(override_settings(WHATSAPP_WEBHOOK_VERIFY_TOKEN='verify-token'))

    def granted(token):
        return {'102345678901234'}

    fakes = {
        'exchange_code': lambda code: {'access_token': 'verify-business-token'},
        'granted_waba_ids': granted,
        'phone_numbers_of': lambda waba_id, token: {'109876543210987'},
        'subscribe_app': lambda waba_id, token: None,
        'unsubscribe_app': lambda waba_id, token: None,
        'register_number': lambda phone_number_id, token, pin=None: '000000',
        'phone_number_details': lambda phone_number_id, token: {
            'display_phone_number': '+1 555 000 1111', 'verified_name': 'Verify Co',
            'quality_rating': 'GREEN',
        },
    }
    for name, fake in fakes.items():
        stack.enter_context(mock.patch(f'whatsapp.graph.{name}', side_effect=fake))
    return stack


def seed_before(name, variables):
    """Create the other people the collection's examples talk about.

    `Create group` adds `bob` and a second number; `Start direct chat` opens a
    thread with `bob`. A person working through the collection would have
    registered those accounts themselves. Making them here is what lets the
    whole chain run unattended - and their numbers are VERIFIED, because
    find_user deliberately refuses to resolve an unverified one.
    """
    from django.contrib.auth.models import User

    if name == 'Start recording':
        # Free has no recording. A person would upgrade first; do that.
        from billing.models import Plan, Subscription

        user = User.objects.get(username='yul')
        Subscription.objects.update_or_create(
            user=user, defaults={'plan': Plan.objects.get(code='premium')}
        )
        return

    if name != 'Create group':
        return
    for username, number in (('bob', '+639170000002'), ('carol', '+639170000003')):
        if User.objects.filter(username=username).exists():
            continue
        user = User.objects.create_user(
            username=username, email=f'{username}@example.com', password='x'
        )
        profile = user.profile
        profile.phone_number = number
        profile.phone_no_verified = True
        profile.email_verified = True
        profile.save()


def seed_after(name, variables):
    """Fill in what a real user reads off a screen or out of an email.

    The collection cannot carry these: the email code is in a mailbox and the
    phone code is held by Telnyx. Reading them from the database is what lets
    the whole chain run unattended.
    """
    from otp.models import EmailOtp

    if name == 'Register':
        row = EmailOtp.objects.order_by('-timestamp').first()
        if row is not None:
            variables['otp_code'] = row.code
    elif name == 'Stop recording':
        # LiveKit reports the finished file by webhook, which Postman cannot
        # send; report it the same way so Get and Delete have a real recording.
        from livekit import api as lk

        from calls.services import apply_egress_info

        apply_egress_info(lk.EgressInfo(
            egress_id='EG_verify',
            status=lk.EgressStatus.EGRESS_COMPLETE,
            file_results=[lk.FileInfo(size=2048, duration=30 * 10**9)],
        ))
    elif name == 'Request phone code':
        # Telnyx owns the SMS code; unconfigured, verification accepts this one.
        from sms.verification import DEV_BYPASS_CODE

        variables['phone_otp_code'] = DEV_BYPASS_CODE


def body_of(item, variables):
    raw = (item.get('request', {}).get('body') or {}).get('raw')
    if raw is None:
        return None, 'json'
    formdata = (item.get('request', {}).get('body') or {}).get('formdata')
    if formdata is not None:
        return {f['key']: f.get('value', '') for f in formdata}, 'multipart'
    return json.loads(substitute(raw, variables)), 'json'


def run():
    collection_arg = sys.argv[1] if len(sys.argv) > 1 else None
    if collection_arg:
        path = HERE / collection_arg
    else:
        candidates = sorted(HERE.glob('loopd_v*.postman_collection.json'))
        if not candidates:
            print('No collection found in postman/.')
            return 1
        path = candidates[-1]

    collection = json.loads(path.read_text())
    print(f'Running {path.name}\n')

    setup_test_environment()
    runner = DiscoverRunner(verbosity=0, interactive=False)
    old_config = runner.setup_databases()
    calls_stack = livekit_stack()
    calls_stack.__enter__()
    whatsapp_fakes = whatsapp_stack()
    whatsapp_fakes.__enter__()

    variables = {v['key']: v['value'] for v in collection.get('variable', [])}
    # The collection ships a placeholder; whatsapp_stack() sets the real one.
    variables['whatsapp_verify_token'] = 'verify-token'
    client = APIClient()
    results = []

    try:
        for folder in collection['item']:
            print(f"  {folder['name']}")
            for item in folder['item']:
                name = item['name']
                if name in SKIP:
                    results.append((name, 'skip', SKIP[name]))
                    print(f'      -    {name}  (skipped: {SKIP[name]})')
                    continue

                seed_before(name, variables)

                request = item['request']
                method = request['method'].lower()
                raw = substitute(request['url']['raw'], variables)
                base = variables.get('base_url', '')
                url_path = raw[len(base):] if raw.startswith(base) else raw

                payload, kind = body_of(item, variables)

                token = variables.get('access_token')
                auth = request.get('auth', {}).get('type')
                if token and auth != 'noauth':
                    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
                else:
                    client.credentials()

                call = getattr(client, method)
                if payload is None:
                    response = call(url_path)
                elif kind == 'multipart':
                    response = call(url_path, payload, format='multipart')
                else:
                    response = call(url_path, payload, format='json')

                code = response.status_code
                try:
                    parsed = response.json()
                except Exception:
                    parsed = {}

                expected = EXPECTED.get(name)
                if expected and code == expected[0]:
                    ok, mark = True, 'ok*'
                elif 200 <= code < 300:
                    ok, mark = True, 'ok '
                else:
                    ok, mark = False, 'FAIL'

                if ok and 200 <= code < 300:
                    capture_from(item, parsed, variables)
                    seed_after(name, variables)

                detail = ''
                if not ok:
                    detail = json.dumps(parsed)[:160]
                results.append((name, mark, f'{code} {detail}'))
                print(f'   {mark}  {code}  {name}  {detail}')
    finally:
        whatsapp_fakes.__exit__(None, None, None)
        calls_stack.__exit__(None, None, None)
        runner.teardown_databases(old_config)
        teardown_test_environment()

    failures = [r for r in results if r[1] == 'FAIL']
    skipped = [r for r in results if r[1] == 'skip']
    print(
        f'\n  {len(results) - len(failures) - len(skipped)} ok, '
        f'{len(failures)} failed, {len(skipped)} skipped'
    )
    if failures:
        print('\n  Failures:')
        for name, _, detail in failures:
            print(f'    {name}: {detail}')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(run())
