"""Live AMI event listener for the admin Monitoring page. Runs as a background asyncio task
inside the same Daphne process as the web server (started/stopped via asgi.py's lifespan
handler) -- no separate systemd service. Passively observes Asterisk's Queue/Agent events and
relays a simplified status per extension to the 'realtime' Channels group; never touches the
call path itself, so a bug here can't affect live calls."""
import asyncio
import datetime
import logging
import os
import re
import time
import uuid

from panoramisk import Manager

logger = logging.getLogger(__name__)

_EXT_RE = re.compile(r'PJSIP/(\w+)')

# Recording only starts once a call is actually answered (AgentConnect for queue calls,
# DialEnd/DialStatus=ANSWER for outbound trunk calls) -- not at the start of the call -- so hold
# music/greetings/ring time never end up in the file. Mirrors agi/router.py's _recording_paths
# (still used for direct-extension inbound, which has no equivalent "answered" ambiguity to
# avoid recording) and core.views._cdr_recording_path (must build the identical path to find
# the file again).
RECORDINGS_DIR = '/var/spool/asterisk/recordings'
_RECORDING_NUMBER_RE = re.compile(r'[^0-9+]')
_RECORDING_NAME_RE = re.compile(r'[^A-Za-z0-9_-]')


async def _recording_now():
    """The app's one fixed timezone (Asia/Dhaka -- see settings.TIME_ZONE, which also matches
    the OS and PostgreSQL now), so recordings file under the real local business day. Still
    `async def` (with nothing to actually await) purely so its call sites don't need to change."""
    from django.utils import timezone
    return timezone.localtime(timezone.now())


def _recording_path(now, queue_name, number, uniqueid):
    folder = os.path.join(RECORDINGS_DIR, now.strftime('%Y'), now.strftime('%m'), now.strftime('%d'))
    queue_name = _RECORDING_NAME_RE.sub('', queue_name or '') or 'direct'
    number = _RECORDING_NUMBER_RE.sub('', number or '') or 'unknown'
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f'{now.strftime("%d%m%Y")}_{queue_name}_{number}_{uniqueid}.wav')

_QUEUE_STATUS_MAP = {
    '1': 'Available',   # Not in use
    '2': 'On Call',      # In use
    '5': 'Unavailable',
    '6': 'Ringing',
    '7': 'On Call',       # Ring+Inuse
    '8': 'On Call',       # On Hold
}

_manager = None

# Last known status per extension -- events only tell us about *changes*, so the Monitoring
# page (rendered by a normal sync Django view, not the WebSocket) reads this snapshot to show
# real status on first load instead of "Unknown" until the next event happens to fire.
_status_by_ext = {}

# One background asyncio task per broadcast currently dialing, keyed by Broadcast.id -- lets
# start_broadcast() refuse to spawn a second loop for a broadcast that's already running (e.g.
# a double click on Start), without needing a separate lock/flag column in the DB.
_broadcast_tasks = {}

# Mirrors Broadcast.status in memory, keyed by id -- _dial_one checks this instead of querying
# the DB before every single dial. A broadcast with thousands of leads would otherwise do one
# extra SELECT per call attempt just to see whether it's still allowed to keep dialing; this
# makes that check free. start_broadcast/pause_broadcast keep it in sync with the DB row.
_broadcast_status = {}

# Auto Dial's queue_name -> AutoDialCampaign.id for the one *running* campaign feeding that
# queue (a queue can only sensibly feed one campaign's leads at a time). Checked on every
# QueueMemberStatus event instead of querying the DB, same reasoning as _broadcast_status --
# this fires on every single agent state change, not just once per lead.
_auto_dial_campaign_by_queue = {}

# ext -> AutoDialAttempt.id currently in flight for that agent -- their extension was just
# rung with the next lead (see _dial_next_auto_dial_lead). Its real outcome (connected/busy/no
# answer) is recorded the instant Asterisk's own Dial() to the lead resolves (see
# _maybe_finish_auto_dial, hooked into the existing DialEnd handler); this entry itself is
# cleared once that ext reports Available again. Keeps a duplicate/retried QueueMemberStatus
# event for one ext from ever dialing two leads for the same agent at once -- the whole point
# of Auto Dial being abandonment-free.
_auto_dial_active_by_ext = {}

# Live bridge membership, kept current purely from BridgeEnter/BridgeLeave events -- lets
# transfer_call() find "whichever channel the agent's phone is currently talking to" without
# ever having to ask Asterisk for it synchronously. bridge_uniqueid -> {channel names};
# channel name -> its current bridge_uniqueid (a channel is in at most one bridge at a time).
_bridge_channels = {}
_channel_bridge = {}

# transfer_call()'s ActionID -> {future, agent_channel, customer_channel} for whichever
# attended-confirm transfer is currently waiting on Asterisk to originate the target leg.
# Resolved (and popped) by _on_originate_response the moment that leg's outcome is known --
# see transfer_call for why the HTTP view can await this directly instead of needing a
# WebSocket push back to the browser.
_pending_transfers = {}


def _phone_matches(a, b):
    """Whichever number is shorter must be a suffix of the other -- same rule as
    agi.router._phone_matches, duplicated here (not imported) since agi/router.py is a
    standalone AGI script that calls django.setup() at import time, which isn't safe to run a
    second time inside this already-initialized ASGI process."""
    if not a or not b:
        return False
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer.endswith(shorter)


async def start_broadcast(broadcast_id):
    """Starts (or resumes) a broadcast's background dialing loop. Returns False (a no-op) if
    one is already running for this broadcast -- prevents a double-click on Start (or a resume
    clicked while the just-paused run is still draining its one in-flight call) from spawning
    two loops that would dial every remaining number twice. The caller surfaces False to the
    admin as a message instead of silently doing nothing, which is what made this look broken
    the first time it happened."""
    from asgiref.sync import sync_to_async
    from core.models import Broadcast

    existing = _broadcast_tasks.get(broadcast_id)
    if existing and not existing.done():
        return False
    await sync_to_async(Broadcast.objects.filter(id=broadcast_id).update)(status='running')
    _broadcast_status[broadcast_id] = 'running'
    _broadcast_tasks[broadcast_id] = asyncio.create_task(_run_broadcast(broadcast_id))
    # A manual Start already reloads the admin's page (redirect after the form POST), so this
    # is only strictly needed for _start_due_scheduled_broadcasts's own background call -- but
    # sent unconditionally so both paths behave identically instead of one silently relying on
    # a page reload that only sometimes happens.
    await _group_send({'kind': 'broadcast_status', 'broadcast_id': broadcast_id, 'status': 'running'})
    return True


async def pause_broadcast(broadcast_id):
    """Flips status to 'paused' -- the loop checks this before every dial (see _dial_one) and
    simply stops sending new Originates once it sees it. Calls already in progress finish on
    their own; nothing gets hung up."""
    from asgiref.sync import sync_to_async
    from core.models import Broadcast

    _broadcast_status[broadcast_id] = 'paused'
    await sync_to_async(Broadcast.objects.filter(id=broadcast_id).update)(status='paused')
    # See start_broadcast's matching push -- same reasoning, needed for
    # _stop_due_scheduled_broadcasts's background call.
    await _group_send({'kind': 'broadcast_status', 'broadcast_id': broadcast_id, 'status': 'paused'})


async def _dial_one(number, broadcast_id, trunk_id, tag, semaphore, progress):
    """One Originate attempt, gated by the semaphore so at most `concurrent_calls` of these
    are ever in flight at once. The status check happens *after* acquiring the semaphore (not
    before creating the task) so a pause mid-broadcast also stops numbers that were already
    queued waiting for a free slot, not just ones not yet dispatched. Reads _broadcast_status
    (in-memory, kept current by start_broadcast/pause_broadcast) instead of querying the DB --
    this runs once per lead, so a large broadcast would otherwise mean one extra SELECT per
    call attempt just to check whether it's still allowed to dial."""
    async with semaphore:
        if _broadcast_status.get(broadcast_id) != 'running' or _manager is None:
            return
        try:
            # Timeout: 30000 only controls Asterisk's *own* ring timeout -- it's no guarantee
            # our side ever gets notified. panoramisk's send_action just awaits a Future that
            # resolves when a matching AMI response arrives, with no client-side timeout of its
            # own; if that response is ever lost or delayed (network hiccup, a protocol edge
            # case with Context/Exten Originate) this await would otherwise hang forever,
            # taking every remaining number in the broadcast down with it (they're all waiting
            # on this same semaphore slot). 35s -- a few seconds past Asterisk's own 30s --
            # gives Asterisk's timeout a chance to fire and produce a clean response first, and
            # only kicks in as a last resort if that never happens.
            await asyncio.wait_for(_manager.send_action({
                'Action': 'Originate',
                'Channel': f'PJSIP/{number}@{trunk_id}',
                # Context/Exten so CDR's dst *usually* captures the real dialed number -- but
                # for a call nobody answers, Asterisk has been observed logging the CDR against
                # the trunk endpoint's own default context instead (dst='s',
                # dcontext='from-external'), since our [broadcast] context's AGI call never
                # actually got to run on an unanswered channel. The number is encoded into
                # userfield itself (tag:number) so it's recoverable from CDR either way -- see
                # _run_broadcast/admin_broadcast_detail_view, which read it from here instead of
                # trusting dst.
                'Context': 'broadcast',
                'Exten': number,
                'Priority': 1,
                'Timeout': 30000,
                # Explicit 'false' (not just omitted), even though this call is meant to block
                # until Asterisk's own direct response -- panoramisk's own multi-response
                # detection (actions.py) unconditionally reads action['async'] once a response
                # message happens to end in "successfully queued", with no default for a
                # missing key. Leaving Async unset here crashed the *entire* AMI connection
                # outright (KeyError -> "Fatal error on transport", full reconnect) the one time
                # a broadcast dial's response text happened to match that -- this is the only
                # Originate call site in this codebase that didn't already set the key.
                'Async': 'false',
                # BROADCAST_ID is all agi/router.py's run_broadcast_message needs -- it looks up
                # the Sound and any BroadcastDigitOption rows straight from the DB itself, so an
                # unlimited/variable number of key mappings never has to be encoded into
                # Originate variables here.
                'Variable': [f'CDR(userfield)={tag}:{number}', f'BROADCAST_ID={broadcast_id}'],
            }), timeout=35)
        except Exception:
            # One dial attempt failing or timing out (AMI hiccup, lost response, malformed
            # data, etc.) must never take down the whole broadcast -- an uncaught exception
            # here would propagate through asyncio.gather() in _run_broadcast and kill that
            # coroutine outright, skipping the "mark completed" step at the end and leaving the
            # broadcast stuck at "Running" forever even after every lead has actually been
            # attempted.
            logger.exception('Originate failed/timed out for %s in broadcast %s', number, broadcast_id)
            return
    # Outside the `async with semaphore` block, deliberately -- the slot is freed the instant
    # Originate returns, so a slow/stuck WebSocket push (Redis hiccup, etc.) below can never
    # starve the next dial waiting on this semaphore. A hard timeout as a second guard: this
    # push is a nice-to-have for the live progress bar, not something that should ever be able
    # to stall the loop that places real calls.
    progress['dialed'] += 1
    try:
        await asyncio.wait_for(_group_send({
            'kind': 'broadcast_progress',
            'broadcast_id': broadcast_id,
            'dialed': progress['dialed'],
            'total': progress['total'],
        }), timeout=5)
    except Exception:
        logger.exception('broadcast_progress push failed for broadcast %s', broadcast_id)


async def _run_broadcast(broadcast_id):
    """The actual dialing loop -- see start_broadcast/pause_broadcast for how it's controlled.
    Skips numbers already dialed this broadcast (found via Cdr.userfield, not a separate
    tracking table -- see Broadcast.tag) so resuming after a pause continues instead of
    re-dialing everyone, and skips Blacklist matches the same way agi/router.py does for
    regular inbound/outbound calls. One attempt per number, no retries -- Busy/No Answer/Failed
    is a final outcome here, same as Answered."""
    from asgiref.sync import sync_to_async
    from django.db.models import Q
    from core.models import Blacklist, Broadcast, Cdr, Lead

    try:
        broadcast = await sync_to_async(Broadcast.objects.get, thread_sensitive=False)(id=broadcast_id)
        tag = broadcast.tag

        # Matches both userfield formats: the plain tag (older rows, before the dialed number
        # was encoded into userfield -- see _dial_one) and tag:number (current). Reading the
        # number out of userfield instead of CDR's dst is what makes this correct regardless of
        # which context Asterisk attributed the CDR to -- a call nobody answers can land
        # dst='s' there (see _dial_one's Originate comment), which would otherwise make the
        # skip check blind to it and redial the same unanswered number on every resume.
        already_dialed = await sync_to_async(
            lambda: {
                uf.split(':', 1)[1] for uf in
                Cdr.objects.filter(Q(userfield=tag) | Q(userfield__startswith=f'{tag}:')).values_list('userfield', flat=True)
                if ':' in uf
            },
            thread_sensitive=False,
        )()
        blacklist_numbers = await sync_to_async(
            lambda: list(Blacklist.objects.values_list('number', flat=True)), thread_sensitive=False
        )()
        lead_numbers = await sync_to_async(
            lambda: list(Lead.objects.filter(lead_list_id=broadcast.lead_list_id).values_list('number', flat=True)),
            thread_sensitive=False,
        )()

        # Matching on the last 7 digits (not a full _phone_matches suffix scan against every
        # blacklist entry) turns this from O(leads x blacklist) Python comparisons into a
        # single O(leads + blacklist) set build -- matters once either list is more than a few
        # hundred rows, and is equivalent for any real phone number (all >= 7 digits).
        blacklist_keys = {b[-7:] for b in blacklist_numbers if b and len(b) >= 7}
        numbers = [
            n for n in lead_numbers
            if n not in already_dialed and n[-7:] not in blacklist_keys
        ]

        # Batched instead of creating one asyncio.Task for every remaining number up front -- a
        # broadcast with tens of thousands of leads would otherwise hold that many Task objects
        # (and pending-coroutine overhead) in memory at once for no benefit, since concurrency
        # is already capped by the semaphore either way. BATCH_SIZE just bounds how many are
        # alive simultaneously; it doesn't change how many dial at once.
        BATCH_SIZE = max(broadcast.concurrent_calls * 4, 50)
        semaphore = asyncio.Semaphore(broadcast.concurrent_calls)
        # Starting count includes numbers already dialed in an earlier run (see already_dialed
        # above) so a resumed broadcast's progress bar continues from where it left off instead
        # of jumping back to 0.
        progress = {'dialed': len(already_dialed), 'total': len(lead_numbers)}
        for i in range(0, len(numbers), BATCH_SIZE):
            if _broadcast_status.get(broadcast_id) != 'running':
                break
            batch = numbers[i:i + BATCH_SIZE]
            await asyncio.gather(*[
                asyncio.create_task(_dial_one(n, broadcast_id, broadcast.trunk_id, tag, semaphore, progress))
                for n in batch
            ])
    except Exception:
        # Whatever broke, the broadcast must not stay stuck on "Running" forever -- see the
        # finally block below, which runs regardless and is what actually decides the final
        # status. This is just so a genuine bug here is visible in the log instead of silent.
        logger.exception('Broadcast %s dialing loop failed', broadcast_id)
    finally:
        # Runs even if the block above raised -- otherwise a single unexpected error (a bug we
        # haven't caught yet, not just a failed Originate -- _dial_one already guards that case
        # on its own) would leave the DB saying "Running" forever with nothing left dialing,
        # which is exactly the "leads are done but it still says Running" symptom this guards.
        # Only auto-completes if still 'running' -- a pause that landed mid-loop should stay
        # "Paused", not get overwritten back to a terminal state once in-flight calls drain.
        final_status = await sync_to_async(
            Broadcast.objects.values_list('status', flat=True).get, thread_sensitive=False
        )(id=broadcast_id)
        if final_status == 'running':
            await sync_to_async(
                Broadcast.objects.filter(id=broadcast_id).update, thread_sensitive=False
            )(status='completed')
            _broadcast_status[broadcast_id] = 'completed'
            # A distinct signal from the per-call 'broadcast_progress' pushes in _dial_one --
            # those fire the instant each Originate returns, which for the *last* call happens
            # before this DB update above has run. A client reloading off that per-call push
            # could hit the server while it's still saying "Running" (dialed_count already at
            # 100%, but status not flipped yet) -- a race that looked like "no live update at
            # all" from a fresh page load's point of view. This only ever fires after the
            # status row is actually committed, so a reload triggered by it is guaranteed to
            # see "Completed".
            try:
                await asyncio.wait_for(_group_send({
                    'kind': 'broadcast_finished',
                    'broadcast_id': broadcast_id,
                }), timeout=5)
            except Exception:
                logger.exception('broadcast_finished push failed for broadcast %s', broadcast_id)


def _extract_ext(interface):
    if not interface:
        return None
    match = _EXT_RE.search(interface)
    return match.group(1) if match else None


async def start_auto_dial(campaign_id):
    """Starts (or resumes) a campaign against its queue. Returns False if a *different*
    campaign is genuinely still running against that same queue -- two campaigns racing to
    dial the same agents would double-book them, so the caller surfaces that to the admin
    instead of silently doing nothing. Also immediately dials any member of the queue who's
    *already* sitting Available right now -- otherwise those agents would only ever get a lead
    the next time their status happens to change for some unrelated reason, which could be
    never."""
    from asgiref.sync import sync_to_async
    from core.models import AutoDialCampaign, QueueMember

    campaign = await sync_to_async(AutoDialCampaign.objects.get, thread_sensitive=False)(id=campaign_id)
    if campaign.status == 'completed':
        # Every lead in the list is already dialed -- there's nothing to start. Without this,
        # the status flip below to 'running' would just immediately flip straight back to
        # 'completed' the moment _dial_next_auto_dial_lead finds no leads left, which looks
        # like Start silently did nothing.
        return False
    existing = _auto_dial_campaign_by_queue.get(campaign.queue_name)
    if existing and existing != campaign_id:
        # The in-memory registration can outlive the campaign it points to (e.g. it was
        # deleted or force-stopped without going through pause_auto_dial) -- don't trust it
        # blindly, confirm against the DB before treating it as a real conflict. Left stale
        # forever otherwise, this queue could never start another campaign again.
        still_running = await sync_to_async(
            AutoDialCampaign.objects.filter(id=existing, status='running').exists, thread_sensitive=False
        )()
        if still_running:
            return False
        del _auto_dial_campaign_by_queue[campaign.queue_name]
    await sync_to_async(
        AutoDialCampaign.objects.filter(id=campaign_id).update, thread_sensitive=False
    )(status='running')
    _auto_dial_campaign_by_queue[campaign.queue_name] = campaign_id

    member_exts = await sync_to_async(
        lambda: [_extract_ext(m.interface) for m in QueueMember.objects.filter(queue_name=campaign.queue_name)],
        thread_sensitive=False,
    )()
    for ext in member_exts:
        if ext and ext not in _auto_dial_active_by_ext and _status_by_ext.get(ext) == 'Available':
            await _dial_next_auto_dial_lead(campaign_id, ext)
    return True


async def pause_auto_dial(campaign_id):
    """Flips status to 'paused' and unregisters the queue -- stops new dials from this campaign,
    but an agent already mid-dial/mid-call for it is left alone (see _auto_dial_tick, which
    still finalizes that one attempt the next time the agent goes Available, it just won't
    start another). A no-op on a campaign that's already 'completed' -- every lead is already
    dialed, so there is nothing left to pause, and flipping it to 'paused' would wrongly imply
    Resume could pick up more leads that don't exist."""
    from asgiref.sync import sync_to_async
    from core.models import AutoDialCampaign

    campaign = await sync_to_async(AutoDialCampaign.objects.get, thread_sensitive=False)(id=campaign_id)
    if campaign.status == 'completed':
        return
    if _auto_dial_campaign_by_queue.get(campaign.queue_name) == campaign_id:
        del _auto_dial_campaign_by_queue[campaign.queue_name]
    await sync_to_async(
        AutoDialCampaign.objects.filter(id=campaign_id).update, thread_sensitive=False
    )(status='paused')


async def _dial_next_auto_dial_lead(campaign_id, ext):
    """Rings ext's own extension (Context=from-internal, Exten=<lead>, Priority=1 -- the same
    dialplan path a manual dial from that agent takes), for every agent alike, WebRTC dashboard
    or physical/softphone -- their own phone ringing *is* the signal, same experience either
    way. Never more than one lead in flight per agent at a time (see _auto_dial_active_by_ext)
    -- there is no predictive over-dialing here, so a lead who answers can never find nobody
    there."""
    from asgiref.sync import sync_to_async
    from django.db import IntegrityError
    from core.models import AutoDialAttempt, AutoDialCampaign, Blacklist, Lead, User

    def prepare():
        campaign = AutoDialCampaign.objects.get(id=campaign_id)
        if campaign.status != 'running':
            return False, None
        agent = User.objects.filter(pjsip_endpoints__id=ext).first()
        if agent is None:
            return False, None
        blacklist_keys = {b[-7:] for b in Blacklist.objects.values_list('number', flat=True) if b and len(b) >= 7}
        # Bounded retry, not a query-and-hope: two agents freeing up at the same instant can
        # both pick the same "next" lead before either's row commits (AutoDialAttempt's
        # unique_together on campaign+number is what actually prevents the double-dial this
        # would otherwise cause) -- on that collision, just re-pick excluding the number that
        # just lost the race and try again, rather than leaving this agent with no lead this
        # round.
        for _ in range(5):
            already = set(AutoDialAttempt.objects.filter(campaign_id=campaign_id).values_list('number', flat=True))
            lead = Lead.objects.filter(lead_list_id=campaign.lead_list_id).exclude(number__in=already).order_by('id').first()
            while lead is not None and lead.number[-7:] in blacklist_keys:
                already.add(lead.number)
                lead = Lead.objects.filter(lead_list_id=campaign.lead_list_id).exclude(number__in=already).order_by('id').first()
            if lead is None:
                AutoDialCampaign.objects.filter(id=campaign_id).update(status='completed')
                return True, None
            try:
                attempt = AutoDialAttempt.objects.create(campaign_id=campaign_id, number=lead.number, agent=agent)
            except IntegrityError:
                continue
            return False, attempt
        return False, None

    completed, attempt = await sync_to_async(prepare, thread_sensitive=False)()
    if attempt is None:
        if completed:
            await _group_send({'kind': 'auto_dial_finished', 'campaign_id': campaign_id})
        return

    _auto_dial_active_by_ext[ext] = attempt.id
    if _manager is not None:
        try:
            # Async: true is the whole point here -- a plain Originate is *synchronous*, its
            # AMI response only arriving once the entire call (agent answers, talks to the
            # lead, hangs up -- potentially minutes) is over or Timeout is hit. Without this,
            # awaiting send_action() would block this entire coroutine -- and, called directly
            # from an AMI event callback (_auto_dial_tick) or from the admin's own Start
            # request (start_auto_dial), would freeze *all* AMI event processing (including the
            # DialEnd this same attempt needs to ever finish) or hang the admin's browser for
            # the length of one live phone call. With Async, this action just acknowledges the
            # origination request itself, in milliseconds; the ringing/answering agent leg
            # proceeds independently and needs no further response here.
            await asyncio.wait_for(_manager.send_action({
                'Action': 'Originate',
                'Channel': f'PJSIP/{ext}',
                'Context': 'from-internal',
                'Exten': attempt.number,
                'Priority': 1,
                'Timeout': 30000,
                'Async': 'true',
                # Shows the lead's number on the agent's phone while it rings -- same as what a
                # manual dial's own screen would show while calling that number, not the
                # agent's own extension. route_outbound() needs the agent's real extension for
                # its queue/route lookup though (the bug this whole block was added for --
                # Asterisk has no real INVITE to derive a Caller ID Number from on an
                # Originated channel, so without setting *some* CallerID here, route_outbound
                # found no matching route and silently hung up softphone agents), so that goes
                # over as a separate channel variable agi/router.py checks first (see
                # AUTO_DIAL_EXT there) instead of overloading CallerID for it.
                'CallerID': attempt.number,
                # CDR(userfield) carries campaign:ext:number -- if the agent never answers this
                # leg at all, agi/router.py's CALLERID(num) reset never runs (it only executes
                # once the dialplan starts, which needs an answer first), and Asterisk
                # separately sometimes attributes that CDR to the endpoint's own default
                # context instead of ours (dst='s'). Either way leaves src/dst wrong or missing
                # on that CDR row, so both the agent's extension and the lead's number are
                # tagged here too (mirrors Broadcast's tag:number pattern) -- see
                # admin_cdr_view, which prefers this over raw src/dst for any AutoDial-tagged
                # row, answered or not.
                'Variable': [f'AUTO_DIAL_EXT={ext}', f'CDR(userfield)=AutoDial-{campaign_id}:{ext}:{attempt.number}'],
            }), timeout=10)
        except Exception:
            logger.exception('Auto-dial Originate failed for %s (campaign %s)', attempt.number, campaign_id)
            _auto_dial_active_by_ext.pop(ext, None)
            await sync_to_async(
                AutoDialAttempt.objects.filter(id=attempt.id).update, thread_sensitive=False
            )(status='failed')
            return

    dialed_count = await sync_to_async(
        AutoDialAttempt.objects.filter(campaign_id=campaign_id).count, thread_sensitive=False
    )()
    await _group_send({'kind': 'auto_dial_progress', 'campaign_id': campaign_id, 'dialed': dialed_count})

    # Self-heals a stuck attempt if the agent's browser never actually places the call (tab
    # closed, WebRTC failure before the INVITE even reaches Asterisk) -- in the normal case
    # _on_dial_end finalizes the real outcome well inside this window, the instant Asterisk's
    # own Dial() to the lead resolves either way.
    asyncio.create_task(_auto_dial_attempt_timeout(attempt.id, ext))


async def _auto_dial_attempt_timeout(attempt_id, ext):
    from asgiref.sync import sync_to_async
    from core.models import AutoDialAttempt

    await asyncio.sleep(60)
    updated = await sync_to_async(
        AutoDialAttempt.objects.filter(id=attempt_id, status='dialing').update, thread_sensitive=False
    )(status='failed')
    if updated and _auto_dial_active_by_ext.get(ext) == attempt_id:
        _auto_dial_active_by_ext.pop(ext, None)


_DIAL_STATUS_TO_AUTO_DIAL = {
    'ANSWER': 'connected', 'BUSY': 'busy', 'NOANSWER': 'no_answer',
    # CANCEL/CONGESTION/CHANUNAVAIL all mean the lead was never reached for reasons that
    # aren't "they didn't pick up" -- CallerID missing/wrong route (the exact bug that made
    # softphone agents' Dial() never even happen) surfaces as CANCEL, for one.
    'CANCEL': 'failed', 'CONGESTION': 'failed', 'CHANUNAVAIL': 'failed',
}


def _set_auto_dial_attempt_status(attempt_id, expected_number, status):
    from core.models import AutoDialAttempt
    qs = AutoDialAttempt.objects.filter(id=attempt_id)
    if expected_number:
        # A different, unrelated manual call this same agent happened to place shouldn't
        # normally be possible (the campaign never dials again until this one clears), but
        # this guards against ever mis-attributing one to the wrong attempt if it somehow did.
        qs = qs.filter(number=expected_number)
    if not qs.update(status=status):
        return None
    return AutoDialAttempt.objects.filter(id=attempt_id).values_list('campaign_id', flat=True).first()


async def _maybe_finish_auto_dial(event):
    """Correlates a from-internal -> from-external DialEnd back to whichever AutoDialAttempt
    is in flight for that agent, if any (see _auto_dial_active_by_ext), and records the real
    outcome -- connected, busy, or no answer -- straight off Asterisk's own DialStatus, no
    guessing from CDR needed. Identifies the agent by the calling Channel (PJSIP/<ext>-...),
    not CallerIDNum -- that field is deliberately the *lead's* number for auto-dial calls (see
    _dial_next_auto_dial_lead), not the agent's extension."""
    ext = _extract_ext(event.get('Channel'))
    if not ext:
        return
    attempt_id = _auto_dial_active_by_ext.get(ext)
    if attempt_id is None:
        return
    status = _DIAL_STATUS_TO_AUTO_DIAL.get(event.get('DialStatus'))
    if status is None:
        return
    from asgiref.sync import sync_to_async
    campaign_id = await sync_to_async(
        _set_auto_dial_attempt_status, thread_sensitive=False
    )(attempt_id, event.get('Exten'), status)
    if campaign_id is not None:
        await _group_send({'kind': 'auto_dial_attempt_done', 'campaign_id': campaign_id})


async def _auto_dial_tick(ext, queue_name):
    """Called whenever ext reports Available in queue_name -- the agent is free, so if
    queue_name is currently feeding a running campaign, dial them the next lead. Also clears
    any leftover in-flight marker for this agent -- by the time Available fires the call is
    genuinely over, so whatever attempt was open (already finalized by _on_dial_end in the
    normal case) is done either way."""
    _auto_dial_active_by_ext.pop(ext, None)
    campaign_id = _auto_dial_campaign_by_queue.get(queue_name)
    if campaign_id is not None:
        await _dial_next_auto_dial_lead(campaign_id, ext)


def get_current_statuses():
    return dict(_status_by_ext)


# Epoch timestamp of when each extension's current status started -- only updated on an actual
# status *change*, not on every repeat event, so the Monitoring page's Duration column can show
# how long an agent has been in their current state instead of resetting every time Asterisk
# re-confirms the same status.
_status_since_by_ext = {}


def get_status_since(ext):
    return _status_since_by_ext.get(ext)


async def set_pause(ext, paused):
    """Settings -> user pause toggle (webrtc.html). Omitting Queue pauses/unpauses the member
    across every queue they belong to -- no need to enumerate memberships ourselves. Asterisk
    fires QueueMemberPause as a side effect, which _on_queue_member_status already relays to
    the Agent Status panel, so no separate broadcast is needed here."""
    if _manager is None:
        return
    await _manager.send_action({
        'Action': 'QueuePause',
        'Interface': f'PJSIP/{ext}',
        'Paused': 'true' if paused else 'false',
    })


# Callers currently waiting in a queue, keyed by Uniqueid -- QueueCallerJoin/Leave fire the
# instant someone enters/exits a queue (answered or abandoned), so this is always accurate
# without polling. Powers the "Live Queue" panel on the User dashboard.
_live_queue_callers = {}


def get_live_queue_callers():
    return sorted(_live_queue_callers.values(), key=lambda c: c['joined_at'])


async def _group_send(data):
    from channels.layers import get_channel_layer
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    await channel_layer.group_send('realtime', {'type': 'realtime_update', 'data': data})


async def _push_status(ext, status):
    # Always broadcasts, even when status hasn't changed -- _periodic_status_refresh relies on
    # this to be the self-healing safety net it's meant to be. A client that connects at the
    # wrong instant (server already corrected but the WebSocket wasn't open yet to receive that
    # broadcast) would otherwise never get corrected: once the backend's own view stabilizes,
    # "only broadcast on change" means no further broadcast ever fires for it. Re-sending
    # identical data every cycle is a small, deliberate cost for guaranteeing every open
    # dashboard is at most one refresh interval away from correct, no matter when it connected.
    if _status_by_ext.get(ext) != status:
        _status_since_by_ext[ext] = time.time()
    _status_by_ext[ext] = status
    await _group_send({'kind': 'status', 'ext': ext, 'status': status, 'since': _status_since_by_ext[ext]})


def _queue_member_status(event):
    if event.get('Paused') == '1':
        return 'Paused'
    return _QUEUE_STATUS_MAP.get(event.get('Status'), 'Unknown')


async def _on_queue_member_status(manager, event):
    ext = _extract_ext(event.get('Interface'))
    if not ext:
        return
    status = _queue_member_status(event)
    logger.info('AMI: %s -> %s', ext, status)
    await _push_status(ext, status)
    if status == 'Available':
        await _auto_dial_tick(ext, event.get('Queue'))


async def _on_queue_member_snapshot(manager, event):
    """QueueStatus action response -- one QueueMember event per member, fired right after we
    connect so the page has real status on first load instead of waiting for the next change."""
    ext = _extract_ext(event.get('Location'))
    if not ext:
        return
    status = _queue_member_status(event)
    logger.info('AMI snapshot: %s -> %s', ext, status)
    await _push_status(ext, status)


async def _agent_queue_name(caller_id):
    """Which queue the dialing agent belongs to, for the outbound recording filename -- same
    membership lookup agi/router.py's route_outbound already does to pick a trunk. An agent in
    more than one queue just gets whichever row comes back first; 'outbound' covers agents with
    no queue membership at all."""
    from asgiref.sync import sync_to_async
    from core.models import QueueMember

    def fetch():
        member = QueueMember.objects.filter(interface=f'PJSIP/{caller_id}').first()
        return member.queue_name if member else None
    return await sync_to_async(fetch)() or 'outbound'


async def _on_dial_end(manager, event):
    """Mirrors _on_agent_event's AgentConnect handling, but for outbound trunk calls -- Queue()
    has its own AgentConnect event for 'the member just answered', but a plain Dial() (what
    agi/router.py's route_outbound uses to ring the trunk) only has DialEnd/DialStatus=ANSWER
    for that same signal. Scoped to from-internal -> from-external specifically so a normal
    agent-to-agent 4-digit call (also from-internal, but never touches a trunk) doesn't trigger
    a recording. Also where an Auto Dial attempt's real outcome (connected/busy/no answer)
    gets recorded -- see _maybe_finish_auto_dial -- since this is the exact same DialEnd every
    manually-placed outbound call produces too, auto-dial or not."""
    context_ok = event.get('Context') == 'from-internal' and event.get('DestContext') == 'from-external'
    if context_ok:
        await _maybe_finish_auto_dial(event)
    if not context_ok or event.get('DialStatus') != 'ANSWER':
        return
    channel = event.get('Channel')
    uniqueid = event.get('Uniqueid')
    if not channel or not uniqueid:
        return
    # Extension from the Channel name (PJSIP/<ext>-...), not CallerIDNum -- that's the lead's
    # number, not the agent's, for an Auto Dial call (see _dial_next_auto_dial_lead), so using
    # it here would misfile the recording under a wrong/nonexistent queue lookup.
    queue_name = await _agent_queue_name(_extract_ext(channel))
    now = await _recording_now()
    path = _recording_path(now, queue_name, event.get('Exten'), uniqueid)
    await manager.send_action({'Action': 'MixMonitor', 'Channel': channel, 'File': path})


async def _on_agent_event(manager, event):
    if event.event == 'AgentConnect':
        # The caller's channel, uniqueid and number are all on this same event -- start
        # recording right now, at the moment the agent actually answers, so hold music/queue
        # greeting never ends up in the file.
        channel = event.get('Channel')
        uniqueid = event.get('Uniqueid')
        if channel and uniqueid:
            now = await _recording_now()
            path = _recording_path(now, event.get('Queue'), event.get('CallerIDNum'), uniqueid)
            await manager.send_action({'Action': 'MixMonitor', 'Channel': channel, 'File': path})

    ext = _extract_ext(event.get('Interface') or event.get('MemberName'))
    if not ext:
        return
    event_map = {
        'AgentCalled': 'Ringing', 'AgentRingNoAnswer': 'Available',
        'AgentConnect': 'On Call', 'AgentComplete': 'Available', 'AgentDump': 'Available',
    }
    status = event_map.get(event.event)
    if not status:
        return
    logger.info('AMI: %s -> %s', ext, status)
    await _push_status(ext, status)


async def _agent_login_payload(ext):
    from asgiref.sync import sync_to_async
    from core.models import Endpoint, QueueMember

    def fetch():
        endpoint = Endpoint.objects.filter(id=ext, assigned_user__role='user').select_related('assigned_user').first()
        if not endpoint:
            return None
        queue_names = list(QueueMember.objects.filter(interface=f'PJSIP/{ext}').values_list('queue_name', flat=True))
        return {
            'kind': 'agent_login', 'ext': ext,
            'name': endpoint.assigned_user.full_name or ext,
            'queues': ', '.join(queue_names) or '—',
        }
    return await sync_to_async(fetch)()


_contact_debounce_tasks = {}


async def _resolve_contact_state(ext):
    from asgiref.sync import sync_to_async
    from core.models import Contact
    exists = await sync_to_async(lambda: Contact.objects.filter(endpoint=ext).exists())()
    if exists:
        payload = await _agent_login_payload(ext)
        if payload:
            logger.info('AMI: %s logged in', ext)
            await _group_send(payload)
            # Don't wait on Asterisk's own qualify cycle (up to qualify_frequency seconds,
            # 60s here) to confirm reachability -- a successful registration already proves
            # the agent is online, so show them Available immediately. A real call event
            # (near-instant, not qualify-dependent) corrects this fast if it's ever wrong.
            await _push_status(ext, 'Available')
    else:
        logger.info('AMI: %s logged out', ext)
        _status_by_ext.pop(ext, None)
        _status_since_by_ext.pop(ext, None)
        await _group_send({'kind': 'agent_logout', 'ext': ext})


async def _debounced_contact_check(ext):
    try:
        await asyncio.sleep(0.8)
    except asyncio.CancelledError:
        return
    await _resolve_contact_state(ext)
    _contact_debounce_tasks.pop(ext, None)


async def _on_contact_status(manager, event):
    # Real-time login/logout for the Monitoring page's row list. A single registration often
    # fires Removed (old contact) then a fresh Reachable/NonQualified (new contact) within
    # milliseconds -- e.g. a browser reconnect replaces its contact URI, not a real logout.
    # Reacting to each raw event would flicker the row, so debounce per extension and check
    # the actual DB state (same source admin_monitoring_view uses) once things settle.
    ext = event.get('EndpointName')
    if not ext:
        return
    existing = _contact_debounce_tasks.get(ext)
    if existing and not existing.done():
        existing.cancel()
    _contact_debounce_tasks[ext] = asyncio.ensure_future(_debounced_contact_check(ext))


_stats_debounce_tasks = {}


async def _push_updated_stats(ext):
    from asgiref.sync import sync_to_async

    def compute():
        from core.views import _today_call_stats
        return _today_call_stats([ext])
    stats = await sync_to_async(compute)()
    logger.info('AMI: %s stats -> %s', ext, stats)
    await _group_send({'kind': 'user_stats', 'ext': ext, **stats})


async def _debounced_stats_update(ext):
    try:
        # Asterisk's CDR write (cdr_adaptive_odbc) lands a moment after Hangup, not
        # synchronously with it -- wait for it to land before recomputing today's totals.
        await asyncio.sleep(2.0)
    except asyncio.CancelledError:
        return
    await _push_updated_stats(ext)
    _stats_debounce_tasks.pop(ext, None)


async def _on_hangup(manager, event):
    ext = _extract_ext(event.get('Channel'))
    if not ext:
        return
    existing = _stats_debounce_tasks.get(ext)
    if existing and not existing.done():
        existing.cancel()
    _stats_debounce_tasks[ext] = asyncio.ensure_future(_debounced_stats_update(ext))


async def _on_bridge_enter(manager, event):
    bridge_id = event.get('BridgeUniqueid')
    channel = event.get('Channel')
    if not bridge_id or not channel:
        return
    _bridge_channels.setdefault(bridge_id, set()).add(channel)
    _channel_bridge[channel] = bridge_id


async def _on_bridge_leave(manager, event):
    bridge_id = event.get('BridgeUniqueid')
    channel = event.get('Channel')
    if channel:
        _channel_bridge.pop(channel, None)
    if bridge_id and bridge_id in _bridge_channels:
        _bridge_channels[bridge_id].discard(channel)
        if not _bridge_channels[bridge_id]:
            del _bridge_channels[bridge_id]


def _find_bridge_peer(ext):
    """The agent's own channel and whichever channel it's currently bridged with (the
    customer), found purely from the live BridgeEnter/BridgeLeave state above -- no AMI
    round-trip needed. Matches by channel-name prefix (PJSIP/<ext>-...) since the agent's
    real channel name carries a unique per-call suffix we don't otherwise know."""
    prefix = f'PJSIP/{ext}-'
    for channel, bridge_id in _channel_bridge.items():
        if channel.startswith(prefix):
            peers = _bridge_channels.get(bridge_id, set()) - {channel}
            if peers:
                return channel, next(iter(peers))
    return None, None


async def _on_originate_response(manager, event):
    """Resolves whichever transfer_call() is waiting on this ActionID. Success means the
    transfer target answered (event['Channel'] is that now-answered leg, parked in Wait() --
    see transfer_call) -- bridge the customer into it and drop the agent's own leg. Anything
    else (busy/no answer/declined/unreachable) means the target never picked up, so the
    agent-customer bridge is left completely untouched -- that call was never touched in the
    first place, only a separate, still-independent leg to the target was ever created."""
    action_id = event.get('ActionID')
    pending = _pending_transfers.pop(action_id, None) if action_id else None
    if not pending:
        return
    future = pending['future']
    if future.done():
        return

    if event.get('Response') != 'Success':
        future.set_result({'ok': False, 'reason': 'no_answer'})
        return

    target_channel = event.get('Channel')
    customer_channel = pending['customer_channel']
    agent_channel = pending['agent_channel']
    try:
        bridge_resp = await manager.send_action({
            'Action': 'Bridge', 'Channel1': customer_channel, 'Channel2': target_channel,
        })
    except Exception:
        bridge_resp = None
    if not bridge_resp or bridge_resp.get('Response') != 'Success':
        # Most likely the customer hung up while the target was still ringing -- nothing left
        # to bridge the now-answered target leg into, so just clean it up instead of leaving
        # it dangling in Wait() forever.
        logger.warning('Transfer bridge failed (%s -> %s), hanging up orphaned target leg', customer_channel, target_channel)
        try:
            await manager.send_action({'Action': 'Hangup', 'Channel': target_channel})
        except Exception:
            pass
        future.set_result({'ok': False, 'reason': 'customer_gone'})
        return

    try:
        await manager.send_action({'Action': 'Hangup', 'Channel': agent_channel})
    except Exception:
        pass
    future.set_result({'ok': True})


def _resolve_outbound_trunk(caller_id):
    """Same trunk-selection precedence as agi.router.route_outbound (the calling agent's own
    queue route, then their own extension route, then the catch-all) -- duplicated rather than
    imported, same reasoning as _phone_matches above: agi/router.py is a standalone AGI script
    that calls django.setup() at import time, unsafe to run a second time inside this
    already-initialized ASGI process. Used by transfer_call when the transfer target isn't a
    registered agent extension, so an external-number attended transfer goes out over whichever
    trunk a manual dial from this same agent would use."""
    from core.models import ROUTE_SOURCE_ANY, Endpoint, Queue, QueueMember, Route

    def trunk_active(trunk_id):
        return bool(trunk_id) and Endpoint.objects.filter(id=trunk_id, is_active=True).exists()

    member = QueueMember.objects.filter(interface=f'PJSIP/{caller_id}').first()
    if member and Queue.objects.filter(name=member.queue_name, is_active=True).exists():
        route = Route.objects.filter(direction='outbound', source_type='queue', source_id=member.queue_name, is_active=True).first()
        if route and trunk_active(route.trunk_id):
            return route.trunk_id
    route = Route.objects.filter(direction='outbound', source_type='extension', source_id=caller_id, is_active=True).first()
    if route and trunk_active(route.trunk_id):
        return route.trunk_id
    route = Route.objects.filter(direction='outbound', source_id=ROUTE_SOURCE_ANY, is_active=True).first()
    if route and trunk_active(route.trunk_id):
        return route.trunk_id
    return None


async def transfer_call(ext, target):
    """Attended-confirm blind transfer: rings `target` as a *separate* leg that never touches
    the agent's live call with the customer -- an internal extension rings directly
    (PJSIP/<target>, same as any agent-to-agent call); anything else is treated as an external
    number and goes out over whichever trunk a manual dial from this agent would use (see
    _resolve_outbound_trunk -- mirrors agi.router.route_outbound's own precedence). Only if/when
    target actually answers does this reach in and swap the customer onto that new leg (via
    AMI's Bridge action) and drop the agent -- if target doesn't answer, the agent/customer
    bridge was never touched at all, so it's exactly as if nothing happened. See
    _on_originate_response for the half of this that runs once Asterisk reports the target
    leg's outcome."""
    if _manager is None:
        return {'ok': False, 'reason': 'ami_unavailable'}

    agent_channel, customer_channel = _find_bridge_peer(ext)
    if not agent_channel or not customer_channel:
        return {'ok': False, 'reason': 'no_active_call'}

    from asgiref.sync import sync_to_async
    from core.models import Endpoint

    is_agent_ext = await sync_to_async(
        Endpoint.objects.filter(id=target, assigned_user__role='user').exists, thread_sensitive=False
    )()
    if is_agent_ext:
        channel = f'PJSIP/{target}'
    else:
        trunk_id = await sync_to_async(_resolve_outbound_trunk, thread_sensitive=False)(ext)
        if not trunk_id:
            return {'ok': False, 'reason': 'no_route'}
        channel = f'PJSIP/{target}@{trunk_id}'

    action_id = f'transfer-{uuid.uuid4().hex}'
    result_future = asyncio.get_event_loop().create_future()
    _pending_transfers[action_id] = {
        'future': result_future, 'agent_channel': agent_channel, 'customer_channel': customer_channel,
    }
    try:
        # Application=Wait (not Context/Exten) -- once target answers, this just holds their
        # channel there doing nothing, instead of running any dialplan on it. We bridge it in
        # ourselves from _on_originate_response the moment we know target really answered,
        # rather than letting the dialplan decide what that channel does next.
        # as_list=False is what actually makes the later OriginateResponse *event* reach
        # _on_originate_response at all -- panoramisk's own multi-response heuristic
        # (actions.py: a message ending in "successfully queued" + Async marks an action
        # 'multi') otherwise treats this Originate as still "open" after its immediate
        # queued-ack, so when the OriginateResponse event arrives later sharing this same
        # ActionID, panoramisk swallows it as a second part of *this* action's own response
        # instead of dispatching it as a normal event. as_list=False short-circuits that
        # heuristic, so the immediate ack alone completes and pops this action, leaving the
        # OriginateResponse event to fall through to the registered event handler like any
        # other event.
        await asyncio.wait_for(_manager.send_action({
            'Action': 'Originate',
            'ActionID': action_id,
            'Channel': channel,
            'Application': 'Wait',
            'Data': '3600',
            'Async': 'true',
            'Timeout': 20000,
            'CallerID': ext,
        }, as_list=False), timeout=10)
    except Exception:
        logger.exception('Transfer Originate failed for %s -> %s', ext, target)
        _pending_transfers.pop(action_id, None)
        return {'ok': False, 'reason': 'originate_failed'}

    try:
        return await asyncio.wait_for(asyncio.shield(result_future), timeout=30)
    except asyncio.TimeoutError:
        # Asterisk's own Originate Timeout (20s) should have already produced an
        # OriginateResponse by now -- this is just a safety net in case that response was ever
        # lost, so the HTTP request doesn't hang forever either way.
        _pending_transfers.pop(action_id, None)
        return {'ok': False, 'reason': 'timeout'}


async def _on_queue_caller_join(manager, event):
    uid = event.get('Uniqueid')
    if not uid:
        return
    _live_queue_callers[uid] = {
        'uniqueid': uid,
        'queue': event.get('Queue'),
        'caller': event.get('CallerIDName') or event.get('CallerIDNum') or 'Unknown',
        'number': event.get('CallerIDNum') or '',
        'position': event.get('Position'),
        'joined_at': time.time(),
    }
    logger.info('AMI: queue join %s -> %s', event.get('CallerIDNum'), event.get('Queue'))
    await _group_send({'kind': 'queue_update', 'callers': get_live_queue_callers()})


async def _on_queue_caller_leave(manager, event):
    uid = event.get('Uniqueid')
    if uid:
        _live_queue_callers.pop(uid, None)
    logger.info('AMI: queue leave %s', event.get('CallerIDNum'))
    await _group_send({'kind': 'queue_update', 'callers': get_live_queue_callers()})


async def _on_queue_entry_snapshot(manager, event):
    """QueueStatus action response for callers already waiting when we (re)connect."""
    uid = event.get('Uniqueid')
    if not uid:
        return
    _live_queue_callers[uid] = {
        'uniqueid': uid,
        'queue': event.get('Queue'),
        'caller': event.get('CallerIDName') or event.get('CallerIDNum') or 'Unknown',
        'number': event.get('CallerIDNum') or '',
        'position': event.get('Position'),
        'joined_at': time.time() - _report_seconds(event.get('Wait')),
    }


def _report_seconds(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def _fetch_initial_snapshot(manager):
    # QueueStatus's response events share the action's ActionID, so panoramisk correlates them
    # into this action's own result list instead of routing them through register_event() --
    # has to be read here, not via a QueueMember event callback.
    response = await manager.send_action({'Action': 'QueueStatus'}, as_list=True)
    _live_queue_callers.clear()
    for event in response:
        if event.get('Event') == 'QueueMember':
            await _on_queue_member_snapshot(manager, event)
        elif event.get('Event') == 'QueueEntry':
            await _on_queue_entry_snapshot(manager, event)


def _on_login(manager):
    # Fires right after auth succeeds -- ask Asterisk for everyone's current status so the
    # Monitoring page has real data immediately, not just on the next status change.
    asyncio.ensure_future(_fetch_initial_snapshot(manager))


_STATUS_REFRESH_INTERVAL = 20
_refresh_task = None


async def _periodic_status_refresh():
    """Safety net: live events are the normal way statuses update, but if the connection ever
    goes quietly stale (event subscription breaks while the socket looks fine -- observed after
    a mid-session process restart) nothing was ever correcting that on its own, and a status
    could stay wrong forever. Re-running the same QueueStatus fetch _on_login already does, on
    a timer, means the worst case is being wrong for one interval instead of indefinitely.
    _push_status only broadcasts what actually changed, so this is cheap when nothing's wrong."""
    while True:
        await asyncio.sleep(_STATUS_REFRESH_INTERVAL)
        if _manager is None:
            continue
        try:
            await _fetch_initial_snapshot(_manager)
        except Exception:
            logger.exception('Periodic status refresh failed')


_SERVER_STATS_INTERVAL = 5
_server_stats_task = None


async def _periodic_server_stats_push():
    """Live feed for the admin Dashboard's Server Performance card -- cpu_interval=None (see
    core.views._server_stats) so this never blocks the shared asyncio event loop AMI events
    also run on; accuracy instead comes from calling it repeatedly on this fixed timer, each
    call measuring CPU usage over the interval since the previous one."""
    from core.views import _server_stats
    while True:
        await asyncio.sleep(_SERVER_STATS_INTERVAL)
        try:
            stats = _server_stats(cpu_interval=None)
            await _group_send({'kind': 'server_stats', **stats})
        except Exception:
            logger.exception('Server stats push failed')


_BROADCAST_SCHEDULE_CHECK_INTERVAL = 30
_broadcast_schedule_task = None


async def _start_due_scheduled_broadcasts():
    """A Broadcast can carry a scheduled_at without ever needing a dedicated 'scheduled'
    status -- it just sits in 'draft' until either an admin presses Start manually (unaffected
    by this field) or this check finds scheduled_at has arrived and calls start_broadcast()
    itself, same as a manual Start would. Only 'draft' rows are ever considered, so a broadcast
    that's already running/paused/completed is never touched here."""
    from asgiref.sync import sync_to_async
    from django.utils import timezone
    from core.models import Broadcast

    due_ids = await sync_to_async(
        lambda: list(Broadcast.objects.filter(
            status='draft', scheduled_at__isnull=False, scheduled_at__lte=timezone.now(),
        ).values_list('id', flat=True)),
        thread_sensitive=False,
    )()
    for broadcast_id in due_ids:
        await start_broadcast(broadcast_id)


async def _stop_due_scheduled_broadcasts():
    """The mirror of _start_due_scheduled_broadcasts -- a 'running' broadcast past its
    scheduled_end_at gets paused automatically, exactly as if an admin had pressed Pause by
    hand (calls already in flight finish on their own; nothing gets hung up). Only 'running'
    rows are considered, so this never touches a broadcast that's already draft/paused/completed."""
    from asgiref.sync import sync_to_async
    from django.utils import timezone
    from core.models import Broadcast

    due_ids = await sync_to_async(
        lambda: list(Broadcast.objects.filter(
            status='running', scheduled_end_at__isnull=False, scheduled_end_at__lte=timezone.now(),
        ).values_list('id', flat=True)),
        thread_sensitive=False,
    )()
    for broadcast_id in due_ids:
        await pause_broadcast(broadcast_id)


async def _periodic_broadcast_schedule_check():
    while True:
        await asyncio.sleep(_BROADCAST_SCHEDULE_CHECK_INTERVAL)
        try:
            await _start_due_scheduled_broadcasts()
            await _stop_due_scheduled_broadcasts()
        except Exception:
            logger.exception('Scheduled broadcast check failed')


async def _recover_stuck_broadcasts():
    """Runs once at process startup. If the app restarted (deploy, crash) while a broadcast
    was 'running', its background dialing task died with it -- the DB row is left saying
    "Running" forever with nothing actually dialing. Flipping it to 'paused' instead of
    silently auto-resuming: a restart might have happened *because* something was wrong, so
    picking up an unattended broadcast automatically felt riskier than requiring an admin to
    look at it and press Start again."""
    from asgiref.sync import sync_to_async
    from core.models import Broadcast

    stuck_ids = await sync_to_async(
        lambda: list(Broadcast.objects.filter(status='running').values_list('id', flat=True)),
        thread_sensitive=False,
    )()
    if not stuck_ids:
        return
    await sync_to_async(
        Broadcast.objects.filter(id__in=stuck_ids).update, thread_sensitive=False
    )(status='paused')
    for broadcast_id in stuck_ids:
        _broadcast_status[broadcast_id] = 'paused'
    logger.warning('Recovered %d broadcast(s) stuck in "running" after restart -> paused', len(stuck_ids))


async def start_ami_listener():
    global _manager, _refresh_task, _broadcast_schedule_task, _server_stats_task
    if _manager is not None:
        return
    _manager = Manager(
        host='127.0.0.1', port=5038,
        username='asterisk_manager_interface', secret='asterisk_manager_interface',
        on_login=_on_login,
    )
    _manager.register_event('QueueMemberStatus', _on_queue_member_status)
    _manager.register_event('QueueMemberPause', _on_queue_member_status)
    _manager.register_event('QueueMember', _on_queue_member_snapshot)
    _manager.register_event('Agent*', _on_agent_event)
    _manager.register_event('DialEnd', _on_dial_end)
    _manager.register_event('BridgeEnter', _on_bridge_enter)
    _manager.register_event('BridgeLeave', _on_bridge_leave)
    _manager.register_event('OriginateResponse', _on_originate_response)
    _manager.register_event('ContactStatus', _on_contact_status)
    _manager.register_event('QueueCallerJoin', _on_queue_caller_join)
    _manager.register_event('QueueCallerLeave', _on_queue_caller_leave)
    _manager.register_event('Hangup', _on_hangup)
    _manager.connect()
    _refresh_task = asyncio.ensure_future(_periodic_status_refresh())
    _broadcast_schedule_task = asyncio.ensure_future(_periodic_broadcast_schedule_check())
    _server_stats_task = asyncio.ensure_future(_periodic_server_stats_push())
    await _recover_stuck_broadcasts()
    logger.info('AMI listener started')


async def stop_ami_listener():
    global _manager, _refresh_task, _broadcast_schedule_task, _server_stats_task
    if _refresh_task is not None:
        _refresh_task.cancel()
        _refresh_task = None
    if _broadcast_schedule_task is not None:
        _broadcast_schedule_task.cancel()
        _broadcast_schedule_task = None
    if _server_stats_task is not None:
        _server_stats_task.cancel()
        _server_stats_task = None
    if _manager is not None:
        _manager.close()
        _manager = None
        logger.info('AMI listener stopped')
