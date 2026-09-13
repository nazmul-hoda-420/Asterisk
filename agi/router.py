#!/var/www/asterisk/venv/bin/python3
"""AGI call router — resolves inbound DID routing and outbound trunk selection for
Settings -> Routing, and internal 4-digit dialing. Spawned fresh by Asterisk for each call
(traditional AGI, no standing service); reads the Route/QueueMember tables directly via the
Django ORM instead of Asterisk's realtime dialplan engine.

Invoked from extensions.conf as:
  AGI(/var/www/asterisk/agi/router.py,inbound)   -- from [from-external]
  AGI(/var/www/asterisk/agi/router.py,outbound)  -- from [from-internal]
"""
import datetime
import os
import re
import sys

sys.path.insert(0, '/var/www/asterisk')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django  # noqa: E402
django.setup()  # noqa: E402

from core.models import ROUTE_SOURCE_ANY, Blacklist, Broadcast, BroadcastDigitOption, Endpoint, Ivr, IvrOption, Queue, QueueMember, Route, Sound  # noqa: E402

_GET_DATA_RESULT_RE = re.compile(r'result=(\S*)')
_GET_VARIABLE_RESULT_RE = re.compile(r'result=1 \((.*)\)')

_WEEKDAY_FIELDS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']  # datetime.weekday(): 0=Mon..6=Sun

RECORDINGS_DIR = '/var/spool/asterisk/recordings'
_RECORDING_NUMBER_RE = re.compile(r'[^0-9+]')
_RECORDING_NAME_RE = re.compile(r'[^A-Za-z0-9_-]')


def _recording_paths(dt, queue_name, number, uniqueid):
    """Year/month/day folders so the directory tree never grows unbounded and a whole day's
    recordings can be retention-deleted in one shot -- see core.views._cdr_recording_path for
    the read-side counterpart, which must build the exact same path from a Cdr row."""
    folder = os.path.join(RECORDINGS_DIR, dt.strftime('%Y'), dt.strftime('%m'), dt.strftime('%d'))
    queue_name = _RECORDING_NAME_RE.sub('', queue_name or '') or 'direct'
    number = _RECORDING_NUMBER_RE.sub('', number or '') or 'unknown'
    filename = f'{dt.strftime("%d%m%Y")}_{queue_name}_{number}_{uniqueid}.wav'
    return folder, filename


def _start_recording(caller_id, uniqueid, queue_name='direct'):
    if not uniqueid:
        return
    folder, filename = _recording_paths(_local_now(), queue_name, caller_id, uniqueid)
    os.makedirs(folder, exist_ok=True)
    agi_exec('MixMonitor', os.path.join(folder, filename))


def _local_now():
    """The app's one fixed timezone (Asia/Dhaka -- see settings.TIME_ZONE, which also matches
    the OS and PostgreSQL now). Used for business-hours and for dating recording files/folders."""
    from django.utils import timezone
    return timezone.localtime(timezone.now())


def _is_business_hours(queue):
    call_time = queue.call_time
    if not call_time:
        return True
    now = _local_now()
    field = _WEEKDAY_FIELDS[now.weekday()]
    start = getattr(call_time, f'{field}_start')
    end = getattr(call_time, f'{field}_end')
    if not start or not end:
        return False
    current = now.time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def read_agi_env():
    env = {}
    for raw in sys.stdin:
        line = raw.strip()
        if line == '':
            break
        if ':' in line:
            key, value = line.split(':', 1)
            env[key.strip()] = value.strip()
    return env


def agi_exec(app, data=''):
    sys.stdout.write(f'EXEC {app} "{data}"\n')
    sys.stdout.flush()
    sys.stdin.readline()


def agi_get_variable(name):
    """Native AGI GET VARIABLE -- unlike agi_callerid/agi_extension (only present for the
    handshake env read at startup), this reads a channel variable set *after* the channel
    already existed, e.g. one set by an AMI Originate's Variable list."""
    sys.stdout.write(f'GET VARIABLE {name}\n')
    sys.stdout.flush()
    response = sys.stdin.readline()
    match = _GET_VARIABLE_RESULT_RE.search(response)
    return match.group(1) if match else ''


def agi_get_data(sound, timeout_ms=5000, max_digits=1):
    """Native AGI GET DATA: plays `sound` and collects up to `max_digits` of DTMF, returning
    whatever was pressed (possibly empty on timeout/no input) -- unlike EXEC, the result comes
    back directly in the AGI response line instead of a channel variable."""
    sys.stdout.write(f'GET DATA "{sound}" {timeout_ms} {max_digits}\n')
    sys.stdout.flush()
    response = sys.stdin.readline()
    match = _GET_DATA_RESULT_RE.search(response)
    return match.group(1) if match else ''


def _trunk_active(trunk_id):
    return bool(trunk_id) and Endpoint.objects.filter(id=trunk_id, is_active=True).exists()


def _phone_matches(a, b):
    """Whichever number is shorter must be a suffix of the other -- matches regardless of
    country code or leading-zero formatting differences, and needs no assumed country/region,
    unlike a fixed-length or E.164 comparison (see core.views._normalize_phone)."""
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer.endswith(shorter)


def _is_blacklisted(caller_id):
    digits = re.sub(r'\D', '', caller_id or '')
    if not digits:
        return False
    return any(_phone_matches(digits, entry) for entry in Blacklist.objects.values_list('number', flat=True))


def run_ivr(ivr_name, caller_id, uniqueid, depth=0):
    if depth > 5:  # guards against a chain of IVRs routing into each other
        return
    ivr = Ivr.objects.filter(name=ivr_name, is_active=True).first()
    if not ivr:
        return

    # Prompts/DTMF collection need a real RTP path -- an unanswered trunk channel only gets
    # early-media audio if the far end explicitly negotiates it, which most providers don't.
    # Progress() (not Answer()) opens that path via 183 Session Progress without marking the
    # channel answered, so CDR's answer time -- and billsec -- still reflect when an agent
    # actually picks up, not when the IVR/queue prompt started.
    if depth == 0:
        agi_exec('Progress')

    attempts = 0
    while attempts <= ivr.max_retries:
        prompt = ivr.invalid_sound if (attempts > 0 and ivr.invalid_sound) else ivr.greeting_sound
        if not prompt:
            return
        digit = agi_get_data(prompt.filename, ivr.timeout * 1000, 1)
        attempts += 1
        if not digit:
            continue  # timeout -- re-prompt

        option = IvrOption.objects.filter(ivr=ivr, digit=digit).first()
        if not option:
            continue  # unmapped key -- re-prompt

        if option.destination_type == 'extension':
            _start_recording(caller_id, uniqueid)
            agi_exec('Dial', f'PJSIP/{option.destination_id},20')
        elif option.destination_type == 'queue':
            if Queue.objects.filter(name=option.destination_id, is_active=True).exists():
                agi_exec('Queue', f'{option.destination_id},t')
        elif option.destination_type == 'ivr':
            run_ivr(option.destination_id, caller_id, uniqueid, depth + 1)
        return


def run_broadcast_message(broadcast_id, caller_id, uniqueid):
    """Voice Broadcast's call leg -- plays the campaign's Sound, and if it has any
    BroadcastDigitOption rows, listens for a single DTMF press during/after playback and routes
    accordingly (a Queue, a specific agent's extension, another IVR menu via run_ivr, or plain
    Hangup). No options at all means
    no digit is ever collected -- just the original plain Playback behavior, unchanged for any
    broadcast that doesn't use this. Unlike run_ivr, there's no re-prompt/invalid-sound/retry
    loop here -- an unmapped key or no press just falls through to the dialplan's own Hangup()
    after this AGI call returns, same as no answer at all."""
    broadcast = Broadcast.objects.filter(id=broadcast_id).select_related('sound').first()
    if not broadcast or not broadcast.sound:
        return
    options = list(BroadcastDigitOption.objects.filter(broadcast=broadcast))
    if not options:
        agi_exec('Playback', broadcast.sound.filename)
        return

    digit = agi_get_data(broadcast.sound.filename, 8000, 1)
    option = next((o for o in options if o.digit == digit), None) if digit else None
    if not option:
        return
    if option.destination_type == 'queue':
        if Queue.objects.filter(name=option.destination_id, is_active=True).exists():
            agi_exec('Queue', f'{option.destination_id},t')
    elif option.destination_type == 'extension':
        agi_exec('Dial', f'PJSIP/{option.destination_id},20')
    elif option.destination_type == 'ivr':
        run_ivr(option.destination_id, caller_id, uniqueid)
    elif option.destination_type == 'hangup' and option.destination_id:
        # destination_id optionally names a Sound to play first (e.g. "thanks for calling") --
        # blank just falls straight through to the dialplan's own Hangup() right after this AGI
        # call, same as an unmapped/timed-out key.
        sound = Sound.objects.filter(id=option.destination_id).first()
        if sound:
            agi_exec('Playback', sound.filename)


def route_inbound(exten, caller_id, uniqueid):
    route = Route.objects.filter(direction='inbound', did=exten, is_active=True).first()
    if not route or not _trunk_active(route.trunk_id):
        return
    if route.destination_type == 'extension':
        # No queue/greeting phase on this path -- ring is effectively immediate, so recording
        # from dial time doesn't pick up anything but silence before the real conversation.
        _start_recording(caller_id, uniqueid)
        agi_exec('Dial', f'PJSIP/{route.destination_id},20')
    elif route.destination_type == 'queue':
        queue = Queue.objects.filter(name=route.destination_id, is_active=True).first()
        if not queue:
            return
        # Progress (not Answer) before any audio -- an unanswered trunk channel has no guaranteed
        # RTP path yet, so the welcome sound and the queue's music on hold would otherwise play
        # into silence. Using Answer() here would also mark the call answered immediately,
        # collapsing CDR's billsec (answer-to-hangup) down to duration (start-to-hangup) and
        # losing the caller's actual wait time.
        #
        # Recording is NOT started here -- ami/ami.py starts it on the AgentConnect AMI event
        # instead, so it only captures audio once an agent actually answers, not the welcome
        # sound/hold music a caller hears while waiting.
        agi_exec('Progress')
        business_hours = _is_business_hours(queue)
        sound = queue.welcome_sound if business_hours else queue.after_hours_sound
        if sound:
            agi_exec('Playback', sound.filename)
        # Outside business hours, no agents are expected to be on duty -- play the after-hours
        # message (if any) and end the call instead of leaving the caller waiting in a queue
        # nobody's staffing.
        if business_hours:
            agi_exec('Queue', f'{route.destination_id},t')
    elif route.destination_type == 'ivr':
        run_ivr(route.destination_id, caller_id, uniqueid)


def route_outbound(exten, caller_id):
    trunk_id = None

    member = QueueMember.objects.filter(interface=f'PJSIP/{caller_id}').first()
    if member and Queue.objects.filter(name=member.queue_name, is_active=True).exists():
        route = Route.objects.filter(direction='outbound', source_type='queue', source_id=member.queue_name, is_active=True).first()
        if route and _trunk_active(route.trunk_id):
            trunk_id = route.trunk_id

    if not trunk_id:
        route = Route.objects.filter(direction='outbound', source_type='extension', source_id=caller_id, is_active=True).first()
        if route and _trunk_active(route.trunk_id):
            trunk_id = route.trunk_id

    if not trunk_id:
        route = Route.objects.filter(direction='outbound', source_id=ROUTE_SOURCE_ANY, is_active=True).first()
        if route and _trunk_active(route.trunk_id):
            trunk_id = route.trunk_id

    if trunk_id:
        # Not started here -- ami/ami.py's DialEnd/DialStatus=ANSWER handler starts it once the
        # far end actually answers, so ringing time never ends up in the file (same reasoning
        # as inbound queue calls waiting on AgentConnect instead of recording from Progress()).
        agi_exec('Dial', f'PJSIP/{exten}@{trunk_id},30')


def main():
    env = read_agi_env()
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    exten = env.get('agi_extension', '')
    caller_id = env.get('agi_callerid', '')
    uniqueid = env.get('agi_uniqueid', '')

    if mode == 'inbound':
        # Blocked numbers never reach the dialplan's IVR/queue/extension routing -- just end the
        # call here, before any Playback/Queue/Dial. No announcement: a DND/blacklist entry means
        # the caller shouldn't get any signal that a person could still be reached this way.
        if _is_blacklisted(caller_id):
            agi_exec('Hangup')
            return
        route_inbound(exten, caller_id, uniqueid)
    elif mode == 'outbound':
        # An AMI-Originated channel (Auto Dial ringing the agent's own extension -- see
        # ami/ami.py) has no real INVITE to derive a Caller ID Number from, so its CallerID is
        # set to the *lead's* number instead (so the agent's phone displays who they're about
        # to reach while it's still ringing, same as a manual dial would show). AUTO_DIAL_EXT
        # carries the agent's real extension separately, for two things: route_outbound()'s
        # queue/extension route lookup, and resetting CALLERID(num) back to the agent's own
        # extension right here -- the ring already happened by this point (dialplan only starts
        # once the agent answers), so this doesn't touch what they saw while it rang, but it
        # does mean the Dial() below reports the agent's extension as CDR src instead of the
        # lead's own number appearing as both src and dst.
        auto_dial_ext = agi_get_variable('AUTO_DIAL_EXT')
        if auto_dial_ext:
            agi_exec('Set', f'CALLERID(num)={auto_dial_ext}')
        route_outbound(exten, auto_dial_ext or caller_id)
    elif mode == 'broadcast':
        broadcast_id = agi_get_variable('BROADCAST_ID')
        if broadcast_id:
            run_broadcast_message(broadcast_id, caller_id, uniqueid)


if __name__ == '__main__':
    main()
