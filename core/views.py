import datetime
import glob
import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Avg, Count, Max, Q, Sum
from django.db.models.functions import TruncDate, TruncHour
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as django_dateformat
from django.utils.dateformat import time_format as django_timeformat
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt

from ami.ami import get_current_statuses, get_live_queue_callers, get_status_since
from core.forms import PHONE_TYPE_ENDPOINT_CONFIG, BroadcastDigitOptionFormSet, CallTimeForm, CrmForm, IvrForm, IvrOptionFormSet, QueueForm, QueueMembersForm, QueueQuickCreateForm, RouteForm, TrunkCreateForm, UserCreateForm
from core.models import Activity, Aor, Auth, AutoDialAttempt, AutoDialCampaign, Blacklist, Broadcast, BroadcastDigitOption, CallTime, Cdr, Cel, Contact, Crm, DropCall, Endpoint, EndpointIdIp, FacebookConversation, FacebookMessage, FacebookSettings, Ivr, IvrOption, Lead, LeadList, MusicOnHold, Queue, QueueLog, QueueMember, Registration, Route, Sound, User, WhatsAppConversation, WhatsAppMessage, WhatsAppSettings
from core.processor import DATE_FORMAT, TIME_FORMAT

# Trunks used to provision with context='from-trunk'; renamed to 'from-external' going forward
# (new/re-enabled trunks -- see _provision_extension callers and admin_settings_registration_
# toggle_view), but historical Cdr/Cel rows Asterisk already wrote still say 'from-trunk', so
# every dcontext comparison checks both instead of just the new name. 'from-whatsapp' is the
# same kind of inbound-trunk context (WhatsApp Business Calling, see admin_settings_
# registration_view) -- dst is the DID there too, with the answering extension in dstchannel.
INBOUND_TRUNK_CONTEXTS = ('from-trunk', 'from-external', 'from-whatsapp')

SOUNDS_DIR = '/var/lib/asterisk/sounds'
MOH_DIR = '/var/lib/asterisk/moh'
RECORDINGS_DIR = '/var/spool/asterisk/recordings'
WHATSAPP_MEDIA_DIR = '/var/spool/asterisk/whatsapp_media'
FACEBOOK_MEDIA_DIR = '/var/spool/asterisk/facebook_media'


def _reload_musiconhold():
    try:
        subprocess.run(['asterisk', '-rx', 'module reload res_musiconhold.so'], capture_output=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _pjsip_reload():
    try:
        subprocess.run(['asterisk', '-rx', 'pjsip reload'], capture_output=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _asterisk_cli(command):
    try:
        result = subprocess.run(['asterisk', '-rx', command], capture_output=True, timeout=10, text=True)
        return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ''


def _get_registration_statuses():
    """Live outbound-registration status per trunk (Registered/Unregistered/Rejected/...), read
    straight from Asterisk's in-memory state via CLI — this isn't stored in ps_registrations,
    which only holds the config Asterisk uses to attempt registration."""
    statuses = {}
    for line in _asterisk_cli('pjsip show registrations').splitlines():
        line = line.strip()
        m = re.match(r'^(\S+)/\S+\s+(\S+)\s+(.+?)(?:\s*\(exp\..*)?$', line)
        if m:
            statuses[m.group(1)] = m.group(3).strip()
    return statuses


def _get_contact_statuses():
    """Live reachability (Avail/Unavail/Unknown) per trunk's static AOR contact — used for
    IP-authentication trunks, which never REGISTER so they have no entry in ps_registrations."""
    statuses = {}
    for line in _asterisk_cli('pjsip show aors').splitlines():
        line = line.strip()
        if not line.startswith('Contact:'):
            continue
        m = re.match(r'^(\S+)/\S+\s+\S+\s+(\S+)', line[len('Contact:'):].strip())
        if m:
            statuses[m.group(1)] = m.group(2)
    return statuses


def _queue_reload():
    try:
        subprocess.run(['asterisk', '-rx', 'queue reload all'], capture_output=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _get_queue_live_status(queue_name):
    """Live snapshot straight from Asterisk's in-memory queue state — calls waiting right now,
    and how many members are available vs. paused/on-a-call/unreachable."""
    output = _asterisk_cli(f'queue show {queue_name}')
    status = {'calls_waiting': 0, 'members_total': 0, 'members_available': 0}
    m = re.search(r'has (\d+) calls?', output)
    if m:
        status['calls_waiting'] = int(m.group(1))
    for line in output.splitlines():
        line = line.strip()
        if 'has taken' not in line:
            continue
        status['members_total'] += 1
        if 'Not in use' in line:
            status['members_available'] += 1
    return status


def _next_extension_number():
    """Auto-assigned extension numbers start at 1001 and fill the lowest free gap (e.g. if 1002 was deleted, the next new user gets 1002 back, not 1004)."""
    numeric_ids = set()
    for ext_id in Endpoint.objects.values_list('id', flat=True):
        try:
            n = int(ext_id)
        except (TypeError, ValueError):
            continue
        if n >= 1001:
            numeric_ids.add(n)
    candidate = 1001
    while candidate in numeric_ids:
        candidate += 1
    return str(candidate)


def _provision_extension(ext_number, full_name, transport, secret):
    """Create the PJSIP auth+aor+endpoint trio for a user. SIP secret = the user's login password, by design (one credential for both)."""
    auth_id = f'{ext_number}-auth'
    Auth.objects.update_or_create(
        id=auth_id,
        defaults=dict(auth_type='userpass', username=ext_number, password=secret, realm='asterisk'),
    )
    Aor.objects.update_or_create(
        id=ext_number,
        defaults=dict(max_contacts=1, remove_existing='yes', qualify_frequency=20),
    )
    return auth_id, secret


def _deprovision_extension(ext_number):
    try:
        endpoint = Endpoint.objects.get(id=ext_number)
    except Endpoint.DoesNotExist:
        return
    Auth.objects.filter(id=endpoint.auth).delete()
    Aor.objects.filter(id__in=(endpoint.aors or '').split()).delete()
    Route.objects.filter(destination_type='extension', destination_id=ext_number).delete()
    Route.objects.filter(direction='outbound', source_type='extension', source_id=ext_number).delete()
    endpoint.delete()


def _post_login_redirect(user):
    return redirect('admin') if user.role == 'admin' else redirect('user_dashboard')


def privacy_policy_view(request):
    """Public, no login -- required by Meta before a WhatsApp app can go Live (App settings ->
    Basic -> Privacy Policy URL). Generic/standard wording for testing; review and tailor it
    before relying on this for a real production launch with real customer data."""
    return render(request, 'privacy_policy.html', {})


def login_view(request):
    if request.user.is_authenticated:
        return _post_login_redirect(request.user)

    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)
        if user is not None:
            # Kick out any other active session for this account before logging in here --
            # WebRTC registers this user's extension per Django session (see
            # core.processor.webrtc_credentials), and the AOR's max_contacts=1 means only one
            # registration can hold the extension's contact slot at a time; a second browser
            # session for the same account trying to register it would just fight the first
            # one for that slot instead of getting a working second line.
            from django.contrib.sessions.models import Session
            had_other_session = False
            for session in Session.objects.filter(expire_date__gte=timezone.now()):
                try:
                    if session.get_decoded().get('_auth_user_id') == str(user.pk):
                        session.delete()
                        had_other_session = True
                except Exception:
                    pass
            if had_other_session:
                # Tells the *old* browser tab to leave immediately instead of only finding out
                # it's logged out the next time it happens to reload -- see user/dashboard.html's
                # WebSocket handler, which checks this against its own user id and redirects.
                from asgiref.sync import async_to_sync
                from channels.layers import get_channel_layer
                channel_layer = get_channel_layer()
                if channel_layer is not None:
                    async_to_sync(channel_layer.group_send)('realtime', {
                        'type': 'realtime_update',
                        'data': {'kind': 'session_kicked', 'user_id': user.id},
                    })
            login(request, user)
            # Close any session left open from before (browser closed without a proper logout,
            # or the "kicked" duplicate-session case above) so Reports -> Activity never sees
            # one left open indefinitely, then open today's.
            Activity.objects.filter(user=user, logout_at__isnull=True).update(logout_at=timezone.now())
            Activity.objects.create(user=user)
            # Agents start every fresh login Paused, not immediately taking calls -- they have
            # to deliberately go Ready on the dashboard first (same AMI QueuePause action as
            # that toggle -- see user_toggle_pause_view/ami.ami.set_pause). Only meaningful for
            # accounts with a phone extension at all; a no-op otherwise.
            endpoint = Endpoint.objects.filter(assigned_user=user).first()
            if endpoint:
                from asgiref.sync import async_to_sync
                from ami.ami import set_pause
                async_to_sync(set_pause)(endpoint.id, True)
            messages.success(request, f"Welcome back, {user.full_name or user.username}!")
            if had_other_session:
                messages.warning(request, 'You were already logged in elsewhere -- that session has been logged out.')
            return _post_login_redirect(user)
        messages.error(request, 'Invalid username or password')
        return render(request, 'auth/login.html')

    return render(request, 'auth/login.html')


@login_required
def admin_settings_view(request):
    return redirect('admin_settings_users')


@login_required
def admin_settings_users_view(request):
    if request.method == 'POST':
        form = UserCreateForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            user = User.objects.create_user(
                username=data['username'], email=data['email'],
                password=data['password'], full_name=data['full_name'],
            )
            user.role = data['role']
            user.is_staff = (data['role'] == 'admin')
            user.plain_password = data['password']
            user.save()

            extension_number = _next_extension_number()
            phone_config = PHONE_TYPE_ENDPOINT_CONFIG[data['phone_type']]
            auth_id, _secret = _provision_extension(
                extension_number, data['full_name'], phone_config['transport'], data['password']
            )
            endpoint_defaults = dict(
                aors=extension_number, auth=auth_id,
                context='from-internal', disallow='all',
                callerid=data['full_name'], assigned_user=user,
                **phone_config,
            )
            Endpoint.objects.update_or_create(id=extension_number, defaults=endpoint_defaults)
            _pjsip_reload()
            messages.success(request, f"User {data['full_name']} created successfully.")
            return redirect('admin_settings_users')
        # invalid: fall through and re-render with the form (and its errors) still open
    else:
        form = UserCreateForm()

    users = User.objects.all().prefetch_related('pjsip_endpoints').order_by('username')
    active_users = sum(1 for u in users if u.is_active)
    registered_extensions = set(
        Contact.objects.filter(expiration_time__gt=int(time.time())).values_list('endpoint', flat=True)
    )
    return render(request, 'admin/settings_users.html', {
        'active': 'settings', 'settings_active': 'users', 'users': users, 'form': form,
        'total_users': len(users), 'active_users': active_users, 'inactive_users': len(users) - active_users,
        'registered_extensions': registered_extensions,
    })


@login_required
def admin_settings_users_delete_view(request, user_id):
    if request.method == 'POST':
        user = get_object_or_404(User, id=user_id)
        for endpoint in user.pjsip_endpoints.all():
            _deprovision_extension(endpoint.id)
        name = user.full_name or user.username
        user.delete()
        _pjsip_reload()
        messages.success(request, f'User {name} deleted.')
    return redirect('admin_settings_users')


@login_required
def admin_settings_users_toggle_view(request, user_id):
    if request.method == 'POST':
        user = get_object_or_404(User, id=user_id)
        user.is_active = not user.is_active
        user.save()

        for endpoint in user.pjsip_endpoints.all():
            if user.is_active:
                Aor.objects.filter(id=endpoint.id).update(max_contacts=1)
            else:
                # Block new registrations and drop any currently-registered softphone immediately.
                Aor.objects.filter(id=endpoint.id).update(max_contacts=0)
                Contact.objects.filter(endpoint=endpoint.id).delete()
        _pjsip_reload()

        messages.success(request, f"User {user.full_name or user.username} is now {'active' if user.is_active else 'inactive'}.")
    return redirect('admin_settings_users')


def _fetch_meta_calling_ip_ranges():
    """Meta's own advice for allow-listing WhatsApp Business Calling traffic: since they don't
    publish a fixed IP list (it rotates), pull their current announced ranges (AS32934) from
    a public routing registry. Falls back to the ranges real test calls were actually observed
    coming from if the registry lookup is unreachable, rather than leaving the trunk with no
    identify at all."""
    try:
        result = subprocess.run(
            ['whois', '-h', 'whois.radb.net', '--', '-i', 'origin', 'AS32934'],
            capture_output=True, text=True, timeout=10,
        )
        ranges = sorted(set(
            line.split()[1] for line in result.stdout.splitlines() if line.startswith('route')
        ))
        if ranges:
            return ranges
    except Exception:
        pass
    return ['173.252.64.0/18', '66.220.144.0/20', '157.240.0.0/16', '31.13.24.0/21', '129.134.0.0/16']


@login_required
def admin_settings_registration_view(request):
    if request.method == 'POST':
        form = TrunkCreateForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            name = data['name']
            host = data['host']
            port = data['port'] or 5060

            if data['registration_type'] == 'whatsapp':
                number = re.sub(r'\D', '', data['whatsapp_number'])
                auth_id = f'{name}-auth'
                Auth.objects.update_or_create(
                    id=auth_id,
                    defaults=dict(auth_type='userpass', username=number, password=data['whatsapp_sip_password'], realm='asterisk'),
                )
                Aor.objects.update_or_create(id=name, defaults=dict(contact='sip:wa.meta.vc:5061%3Btransport=tls', qualify_frequency=0))
                Endpoint.objects.update_or_create(
                    id=name,
                    defaults=dict(
                        transport='transport-tls', context='from-whatsapp', disallow='all', allow='opus',
                        # WhatsApp Business Calling is a server-to-server SIP trunk, not a browser
                        # WebRTC client -- Meta's own INVITE offers plain SDES-SRTP (a=crypto, no
                        # ICE/DTLS at all). Answering with DTLS (a=setup/a=fingerprint, what the
                        # ice_support/rtcp_mux/use_avpf/webrtc=yes bundle below used to produce)
                        # gives Meta an answer format it doesn't recognize -- it never ACKs, and
                        # Asterisk gives up ~32s later with no BYE. media_encryption='sdes' matches
                        # what Meta actually sent; the other WebRTC-only options are irrelevant
                        # (no ICE candidates in Meta's offer either) so they're left at their
                        # normal defaults instead of being force-enabled.
                        media_encryption='sdes', direct_media='no',
                        auth=auth_id, outbound_auth=auth_id, aors=name,
                    ),
                )
                EndpointIdIp.objects.update_or_create(
                    id=f'{name}-identify',
                    defaults=dict(endpoint=name, match=','.join(_fetch_meta_calling_ip_ranges())),
                )
                Registration.objects.filter(id=name).delete()
                _pjsip_reload()
                messages.success(request, f'WhatsApp trunk "{name}" saved. Set up where inbound calls ring on Settings → Routing.')
                return redirect('admin_settings_registration')

            Endpoint.objects.update_or_create(
                id=name,
                # No static `callerid` here -- Asterisk always substitutes a configured
                # endpoint callerid for the identity of every INBOUND call too (trust_id_inbound
                # only overrides this for P-Asserted-Identity/Remote-Party-ID, not the plain
                # From header most providers actually send), which hid the real caller's number.
                defaults=dict(transport='transport-udp', context='from-external', disallow='all', allow='ulaw,alaw', trust_id_inbound='yes'),
            )
            Aor.objects.update_or_create(id=name, defaults=dict(contact=f'sip:{host}:{port}', max_contacts=1, qualify_frequency=60))

            if data['registration_type'] == 'userpass':
                auth_id = f'{name}-auth'
                Auth.objects.update_or_create(
                    id=auth_id,
                    defaults=dict(auth_type='userpass', username=data['username'], password=data['password'], realm=host),
                )
                # `auth` (not `outbound_auth`) is what makes Asterisk challenge INBOUND
                # requests matched to this endpoint. Providers register-and-deliver calls by
                # IP trust and don't answer a 401 on their own inbound INVITE, so setting it
                # here silently kills every inbound call with no error beyond a 401/ACK in the
                # SIP trace. Only outbound_auth (credentials WE send on REGISTER) belongs here.
                Endpoint.objects.filter(id=name).update(aors=name, auth='', outbound_auth=auth_id)
                Registration.objects.update_or_create(
                    id=name,
                    defaults=dict(
                        server_uri=f'sip:{host}:{port}', client_uri=f'sip:{data["username"]}@{host}:{port}',
                        contact_user=data['username'], transport='transport-udp', outbound_auth=auth_id,
                        expiration=3600, retry_interval=60, max_retries=10,
                    ),
                )
                # Registering out doesn't by itself let inbound INVITEs from the provider
                # match this endpoint -- chan_pjsip still needs an explicit identify. Most
                # providers send calls from the same host they register to, so match on that.
                EndpointIdIp.objects.update_or_create(id=f'{name}-ip-0', defaults=dict(endpoint=name, match=host))
            else:
                Endpoint.objects.filter(id=name).update(aors=name, auth='', outbound_auth='')
                EndpointIdIp.objects.filter(endpoint=name).delete()
                for i, ip in enumerate(part.strip() for part in data['ip_addresses'].split(',') if part.strip()):
                    EndpointIdIp.objects.create(id=f'{name}-ip-{i}', endpoint=name, match=ip)
                Registration.objects.filter(id=name).delete()
                Auth.objects.filter(id=f'{name}-auth').delete()

            _pjsip_reload()
            messages.success(request, f'Trunk "{name}" saved.')
            return redirect('admin_settings_registration')
        else:
            for field_errors in form.errors.values():
                for error in field_errors:
                    messages.error(request, error)
    else:
        form = TrunkCreateForm()

    trunks = Endpoint.objects.filter(assigned_user__isnull=True).order_by('id')
    registrations = {r.id: r for r in Registration.objects.filter(id__in=[t.id for t in trunks])}
    aors = {a.id: a for a in Aor.objects.filter(id__in=[t.id for t in trunks])}
    auths = {a.id: a for a in Auth.objects.filter(id__in=[f'{t.id}-auth' for t in trunks])}
    ip_identifies = {}
    for row in EndpointIdIp.objects.all():
        ip_identifies.setdefault(row.endpoint, []).append(row.match)
    registration_statuses = _get_registration_statuses()
    contact_statuses = _get_contact_statuses()

    trunk_rows = []
    for t in trunks:
        reg = registrations.get(t.id)
        ips = ip_identifies.get(t.id, [])
        aor = aors.get(t.id)
        auth = auths.get(f'{t.id}-auth')
        # Keyed off Auth (not Registration) -- Registration is deleted while a userpass trunk
        # is deactivated (stops outbound REGISTER attempts), but Auth stays intact, so this
        # stays correct across the active/inactive toggle. Userpass trunks also carry an
        # EndpointIdIp row now (for inbound identify), so `ips` alone can't disambiguate.
        if t.context == 'from-whatsapp':
            reg_type = 'whatsapp'
        elif auth:
            reg_type = 'userpass'
        elif ips:
            reg_type = 'ip'
        else:
            reg_type = 'unknown'
        if reg_type == 'userpass':
            status = registration_statuses.get(t.id, 'Unknown')
        elif reg_type == 'ip':
            status = contact_statuses.get(t.id, 'Unavail')
        elif reg_type == 'whatsapp':
            # No REGISTER and no reliable qualify/OPTIONS ping here (the %3B-escaped contact
            # needed so realtime doesn't split it into two contacts -- see the Aor creation
            # comment -- isn't valid to build a real outbound OPTIONS request from), so there's
            # no live reachability signal to show. is_active is the only honest status: it's
            # either configured to accept calls or it isn't.
            status = 'Active' if t.is_active else 'Inactive'
        else:
            status = 'Unknown'
        if reg_type == 'whatsapp':
            host = 'wa.meta.vc'
        else:
            host = reg.server_uri if reg else (aor.contact if aor else None)
            if host and host.startswith('sip:'):
                host = host[len('sip:'):]
        trunk_rows.append({
            'endpoint': t,
            'status': status,
            'registration_type': reg_type,
            'registration': reg,
            'ips': ips,
            'host': host,
            'username': auth.username if auth else None,
            'password': auth.password if auth else None,
        })

    total_trunks = len(trunk_rows)
    active_trunks = sum(1 for t in trunks if t.is_active)
    inactive_trunks = total_trunks - active_trunks

    return render(request, 'admin/settings_registration.html', {
        'active': 'settings', 'settings_active': 'registration', 'form': form, 'trunk_rows': trunk_rows,
        'total_trunks': total_trunks, 'active_trunks': active_trunks, 'inactive_trunks': inactive_trunks,
    })


@login_required
def admin_settings_registration_delete_view(request, trunk_id):
    if request.method == 'POST':
        endpoint = get_object_or_404(Endpoint, id=trunk_id, assigned_user__isnull=True)
        Auth.objects.filter(id=endpoint.auth).delete()
        Aor.objects.filter(id=trunk_id).delete()
        Registration.objects.filter(id=trunk_id).delete()
        EndpointIdIp.objects.filter(endpoint=trunk_id).delete()
        Route.objects.filter(trunk_id=trunk_id).delete()
        endpoint.delete()
        _pjsip_reload()
        messages.success(request, f'Trunk "{trunk_id}" deleted.')
    return redirect('admin_settings_registration')


@login_required
def admin_settings_registration_toggle_view(request, trunk_id):
    if request.method == 'POST':
        endpoint = get_object_or_404(Endpoint, id=trunk_id, assigned_user__isnull=True)
        endpoint.is_active = not endpoint.is_active
        auth_id = f'{trunk_id}-auth'

        if endpoint.is_active:
            # WhatsApp trunks are the only ones whose Aor points at Meta's fixed SIP domain --
            # a reliable, unchanging marker to restore the right context on re-activation.
            is_whatsapp = Aor.objects.filter(id=trunk_id, contact__startswith='sip:wa.meta.vc').exists()
            endpoint.context = 'from-whatsapp' if is_whatsapp else 'from-external'
            endpoint.save()
            if is_whatsapp:
                _pjsip_reload()
                messages.success(request, f"Trunk \"{trunk_id}\" is now active.")
                return redirect('admin_settings_registration')
            # Username/password trunks: restore outbound registration if it's missing (it was
            # removed on deactivate). Rebuilt from Auth/Aor, so nothing entered by the admin
            # is lost while the trunk was disabled.
            auth = Auth.objects.filter(id=auth_id).first()
            aor = Aor.objects.filter(id=trunk_id).first()
            if auth and aor and aor.contact and not Registration.objects.filter(id=trunk_id).exists():
                Registration.objects.create(
                    id=trunk_id, server_uri=aor.contact,
                    client_uri=f'sip:{auth.username}@{aor.contact[len("sip:"):]}',
                    contact_user=auth.username, transport='transport-udp', outbound_auth=auth_id,
                    expiration=3600, retry_interval=60, max_retries=10,
                )
        else:
            endpoint.context = 'trunk-disabled'
            endpoint.save()
            # Stops outbound REGISTER attempts while disabled. Auth/Aor/identify rows stay
            # intact so re-enabling doesn't require re-entering anything.
            Registration.objects.filter(id=trunk_id).delete()

        _pjsip_reload()
        messages.success(request, f"Trunk \"{trunk_id}\" is now {'active' if endpoint.is_active else 'inactive'}.")
    return redirect('admin_settings_registration')


CALL_TIME_DAY_LABELS = [
    ('sat', 'Sat'), ('sun', 'Sun'), ('mon', 'Mon'), ('tue', 'Tue'),
    ('wed', 'Wed'), ('thu', 'Thu'), ('fri', 'Fri'),
]


@login_required
def admin_settings_call_time_view(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        ct_id = request.POST.get('id') or None
        if action == 'delete':
            schedule = CallTime.objects.filter(id=ct_id).first() if ct_id else None
            if schedule:
                Queue.objects.filter(call_time=schedule).update(call_time=None)
                schedule.delete()
                messages.success(request, 'Schedule deleted.')
        else:
            instance = CallTime.objects.filter(id=ct_id).first() if ct_id else None
            form = CallTimeForm(request.POST, instance=instance)
            if form.is_valid():
                form.save()
                messages.success(request, 'Schedule saved.')
            else:
                messages.error(request, ' '.join(f'{f}: {e[0]}' for f, e in form.errors.items()))
        return redirect('admin_settings_call_time')

    time_fmt = TIME_FORMAT
    schedules = []
    for ct in CallTime.objects.order_by('name'):
        days = []
        edit_payload = {'id': ct.id, 'name': ct.name}
        for code, label in CALL_TIME_DAY_LABELS:
            start, end = getattr(ct, f'{code}_start'), getattr(ct, f'{code}_end')
            days.append({
                'label': label,
                'display': f'{django_timeformat(start, time_fmt)}–{django_timeformat(end, time_fmt)}' if start and end else None,
            })
            edit_payload[f'{code}_start'] = start.strftime('%H:%M') if start else ''
            edit_payload[f'{code}_end'] = end.strftime('%H:%M') if end else ''
        schedules.append({'id': ct.id, 'name': ct.name, 'days': days, 'edit_payload': edit_payload, 'dom_id': f'ct-data-{ct.id}'})

    return render(request, 'admin/settings_call_time.html', {
        'active': 'settings', 'settings_active': 'call_time',
        'form': CallTimeForm(), 'schedules': schedules, 'day_codes': CALL_TIME_DAY_LABELS,
    })


REPORT_PERIODS = {
    'today': lambda now: now.replace(hour=0, minute=0, second=0, microsecond=0),
    '7': lambda now: now - datetime.timedelta(days=7),
    '30': lambda now: now - datetime.timedelta(days=30),
    'month': lambda now: now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
}


def _report_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _agent_performance_rows(logs):
    """Aggregate per-agent stats from a QueueLog queryset already scoped to a date range and
    to real queues (queryname != 'NONE'). `agent` carries the QueueMember's membername (or
    interface if unset). COMPLETEAGENT/COMPLETECALLER's data2 is talk time; RINGNOANSWER marks
    a call this agent was offered but didn't pick up."""
    stats = {}
    # Only call-completion events carry the resolved membername -- member-management events
    # (ADDMEMBER/REMOVEMEMBER, ...) log the raw interface ("PJSIP/1001") instead, which would
    # otherwise show up as zero-activity ghost rows alongside the agent's real one.
    for agent, event, data2 in logs.exclude(agent='NONE').filter(event__in=('COMPLETEAGENT', 'COMPLETECALLER', 'RINGNOANSWER')).values_list('agent', 'event', 'data2'):
        stat = stats.setdefault(agent, {'name': agent, 'calls': 0, 'missed': 0, 'talk_total': 0, 'talk_n': 0})
        if event in ('COMPLETEAGENT', 'COMPLETECALLER'):
            stat['calls'] += 1
            talk_sec = _report_int(data2)
            if talk_sec is not None:
                stat['talk_total'] += talk_sec
                stat['talk_n'] += 1
        elif event == 'RINGNOANSWER':
            stat['missed'] += 1

    rows = []
    for stat in stats.values():
        total_attempts = stat['calls'] + stat['missed']
        rows.append({
            'name': stat['name'], 'calls': stat['calls'], 'missed': stat['missed'],
            'avg_talk': stat['talk_total'] / stat['talk_n'] if stat['talk_n'] else 0,
            'talk_total': stat['talk_total'],
            'answer_rate': round(stat['calls'] / total_attempts * 100) if total_attempts else 0,
        })
    rows.sort(key=lambda r: r['calls'], reverse=True)
    return rows


def _agent_activity_rows(day):
    """First Login/Last Logout/Total Login Time/Total Pause Time for one single calendar day --
    unlike _agent_performance_rows' stats, these only have a clear meaning for one specific day,
    not a date range (see admin_activity_view). Login/logout comes from Activity -- the
    real web-dashboard login/logout (see login_view/logout_view) -- deliberately *not* from
    QueueLog's ADDMEMBER/REMOVEMEMBER, which only fire when a PJSIP Contact actually
    registers/unregisters and can stay silent for days at a time if an agent's phone/softphone
    never drops its registration between dashboard sessions. Multiple login/logout cycles in
    one day are handled by summing every session's duration and tracking the earliest login /
    latest logout across all of them. Pause time still pairs sequential QueueLog PAUSE/UNPAUSE
    events, the same way set_pause() actually triggers them -- that part doesn't have the same
    problem, since a pause/unpause pair only ever exists because it was just triggered."""
    # Naive, matching wall-clock -- QueueLog.time is a plain string Asterisk writes in local
    # time (see _queue_log_time_display), not UTC-aware like Cdr, so subtracting against an
    # aware "now" here would be comparing two different clocks.
    now = timezone.localtime(timezone.now()).replace(tzinfo=None)
    is_today = day == now.date()

    activity = {}

    sessions = Activity.objects.filter(login_at__date=day).select_related('user').order_by('login_at')
    for sess in sessions:
        name = sess.user.full_name or sess.user.username
        row = activity.setdefault(name, {'first_login': None, 'last_logout': None, 'total_login': datetime.timedelta(), 'still_open': False})
        login_local = timezone.localtime(sess.login_at).replace(tzinfo=None)
        if row['first_login'] is None:
            row['first_login'] = login_local
        if sess.logout_at:
            logout_local = timezone.localtime(sess.logout_at).replace(tzinfo=None)
            row['last_logout'] = logout_local
            row['still_open'] = False
            row['total_login'] += logout_local - login_local
        else:
            row['still_open'] = True
            if is_today:
                row['total_login'] += now - login_local

    day_str = day.isoformat()
    pause_rows = list(
        QueueLog.objects.filter(
            time__gte=day_str, time__lte=day_str + ' 23:59:59.999999', event__in=('PAUSE', 'UNPAUSE'),
        ).order_by('time').values_list('time', 'agent', 'event')
    )

    def parse_dt(raw):
        # Keep microsecond precision here (unlike _queue_log_time_display's display-only
        # parsing) -- a Pause landing in the same second as login needs sub-second ordering
        # to tell "just paused right after logging in" apart from "stale pause from before
        # today's login", which the pending_start check below depends on.
        try:
            return datetime.datetime.strptime(raw, '%Y-%m-%d %H:%M:%S.%f')
        except (ValueError, AttributeError):
            try:
                return datetime.datetime.strptime(raw.split('.')[0], '%Y-%m-%d %H:%M:%S')
            except (ValueError, AttributeError):
                return None

    pause_events = {}
    for raw_time, agent, event in pause_rows:
        dt = parse_dt(raw_time)
        if not dt or not agent:
            continue
        pause_events.setdefault(agent, []).append((event, dt))

    for membername, events in pause_events.items():
        events.sort(key=lambda e: e[1])
        total_pause = datetime.timedelta()
        pending_start = None
        for ev, dt in events:
            if ev == 'PAUSE':
                pending_start = dt
            elif ev == 'UNPAUSE' and pending_start:
                total_pause += dt - pending_start
                pending_start = None
        if pending_start:
            # A trailing Pause with no matching Unpause -- only keep counting up to "now" while
            # the agent is actually still logged in. Once they've logged out there's no one
            # left to be paused, so the clock should stop at their last logout instead of
            # growing forever on every page reload.
            login_row = activity.get(membername, {})
            first_login = login_row.get('first_login')
            if first_login and pending_start < first_login:
                # This Pause started *before* the agent's first login that day -- it's a stale
                # pause left dangling from some earlier, untracked session, not something that
                # happened during today's tracked login window. Attributing it to today's (much
                # later) logout would count hours that were never actually spent paused while
                # logged in, so it's left out rather than force-fit against an unrelated session.
                pass
            elif login_row.get('still_open'):
                if is_today:
                    total_pause += now - pending_start
            elif login_row.get('last_logout'):
                total_pause += max(login_row['last_logout'] - pending_start, datetime.timedelta())
            elif is_today:
                # No login/logout data for this agent at all today -- nothing to bound it
                # against, so fall back to "now" same as before.
                total_pause += now - pending_start
        activity.setdefault(membername, {})['total_pause'] = total_pause

    return activity


def _agent_activity_timeline(day, agent_name):
    """Chronological Login/Pause/Unpause/Logout events for one agent on one day -- powers the
    CEL-style timeline view on the Activity report (see admin_activity_view). Same two sources
    as _agent_activity_rows (Activity for Login/Logout, QueueLog for Pause/Unpause), just
    interleaved into one ordered list instead of aggregated into totals."""
    events = []

    sessions = Activity.objects.filter(login_at__date=day).select_related('user').order_by('login_at')
    for sess in sessions:
        name = sess.user.full_name or sess.user.username
        if name != agent_name:
            continue
        events.append({'type': 'LOGIN', 'time': timezone.localtime(sess.login_at).replace(tzinfo=None)})
        if sess.logout_at:
            events.append({'type': 'LOGOUT', 'time': timezone.localtime(sess.logout_at).replace(tzinfo=None)})

    day_str = day.isoformat()
    pause_rows = QueueLog.objects.filter(
        time__gte=day_str, time__lte=day_str + ' 23:59:59.999999',
        event__in=('PAUSE', 'UNPAUSE'), agent=agent_name,
    ).order_by('time').values_list('time', 'event')
    for raw_time, event in pause_rows:
        # Full microsecond precision (unlike display-only parsing elsewhere) -- a Pause landing
        # in the same second as a Login/Logout still needs correct sub-second ordering here.
        try:
            dt = datetime.datetime.strptime(raw_time, '%Y-%m-%d %H:%M:%S.%f')
        except (ValueError, TypeError):
            try:
                dt = datetime.datetime.strptime(raw_time.split('.')[0], '%Y-%m-%d %H:%M:%S')
            except (ValueError, AttributeError):
                continue
        events.append({'type': event, 'time': dt})

    events.sort(key=lambda e: e['time'])
    for e in events:
        e['display_time'] = django_timeformat(e['time'], TIME_FORMAT)
    return events


def _canonical_cdrs(queryset):
    """Asterisk forks a second, near-empty CDR row per call (duration=0, dstchannel blank) at
    Hangup, alongside the real one written when Dial() completes -- both share `linkedid`.
    Collapse to the most informative row (highest duration) per linkedid so a call isn't
    double-counted anywhere calls are listed or aggregated."""
    canonical_ids = (
        queryset
        .exclude(linkedid__isnull=True).exclude(linkedid='')
        .order_by('linkedid', '-duration', '-id')
        .distinct('linkedid')
        .values_list('id', flat=True)
    )
    return Cdr.objects.filter(id__in=list(canonical_ids))


def _server_stats(cpu_interval=0.1):
    """Snapshot of the host's own CPU/RAM/disk load -- purely for the admin Dashboard's own
    "how loaded is the server itself" card, unrelated to any call/agent data. cpu_interval=0.1
    (the default, used for this one-off per-page-load call) is a short blocking sample -- psutil
    returns 0.0 on a bare first call with no prior baseline, so it needs *some* wait; a small,
    one-time cost here. ami.ami's periodic live-push instead passes cpu_interval=None (instant,
    non-blocking -- accurate because it's called repeatedly on a timer, each call measuring the
    delta since the last one) so that a blocking 0.1s sample never runs inside its own shared
    asyncio event loop, where call-handling events need to stay responsive."""
    import psutil
    disk = psutil.disk_usage('/')
    mem = psutil.virtual_memory()
    gb = 1024 ** 3
    return {
        'cpu_percent': psutil.cpu_percent(interval=cpu_interval),
        'ram_percent': mem.percent,
        'ram_used_gb': mem.used / gb,
        'ram_total_gb': mem.total / gb,
        'disk_percent': disk.percent,
        'disk_used_gb': disk.used / gb,
        'disk_total_gb': disk.total / gb,
    }


def _live_queue_breakdown():
    """Live Queue's one aggregate count, split out per queue -- same underlying callers
    (get_live_queue_callers already carries each one's queue name), just grouped."""
    counts = Counter(c['queue'] for c in get_live_queue_callers() if c.get('queue'))
    return [{'name': name, 'count': n} for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _today_sla():
    """% of today's queue calls answered within each queue's own target answer time
    (Queue.servicelevel, seconds) -- standard call-center SLA = answered-in-time / (answered +
    abandoned). None if there were no queue calls at all today (nothing to compute a rate from)."""
    today = _local_today().isoformat()
    thresholds = dict(Queue.objects.values_list('name', 'servicelevel'))
    logs = QueueLog.objects.filter(
        time__gte=today, time__lte=today + ' 23:59:59.999999', event__in=('CONNECT', 'ABANDON'),
    ).values_list('event', 'queuename', 'data1', 'data3')
    total = within_target = 0
    for event, queuename, data1, data3 in logs:
        waited = _report_int(data1) if event == 'CONNECT' else _report_int(data3)
        if waited is None:
            continue
        total += 1
        if waited <= thresholds.get(queuename, 60):
            within_target += 1
    return round(within_target * 100 / total, 1) if total else None


def _today_queue_wait_stats():
    """Today's average queue wait time (seconds, across CONNECT+ABANDON) and abandoned count --
    same event fields _today_sla() reads (CONNECT's Waited is data1, ABANDON's is data3, see
    _QUEUE_LOG_FIELD_LABELS)."""
    today = _local_today().isoformat()
    logs = QueueLog.objects.filter(
        time__gte=today, time__lte=today + ' 23:59:59.999999', event__in=('CONNECT', 'ABANDON'),
    ).values_list('event', 'data1', 'data3')
    waits = []
    abandoned = 0
    for event, data1, data3 in logs:
        waited = _report_int(data1) if event == 'CONNECT' else _report_int(data3)
        if waited is not None:
            waits.append(waited)
        if event == 'ABANDON':
            abandoned += 1
    avg_wait = round(sum(waits) / len(waits)) if waits else 0
    return {
        'avg_wait_display': f'{avg_wait // 60:02d}:{avg_wait % 60:02d}',
        'abandoned_today': abandoned,
    }


def _longest_waiting_caller():
    """The live queue caller who's been waiting the longest right now, or None if the queue is
    empty -- get_live_queue_callers() already carries each one's join time."""
    callers = get_live_queue_callers()
    if not callers:
        return None
    longest = min(callers, key=lambda c: c['joined_at'])
    return {
        'caller': longest['caller'], 'queue': longest['queue'],
        'wait_seconds': int(time.time() - longest['joined_at']),
    }


def _active_broadcasts():
    """Broadcasts currently running or paused, with the same dialed/lead_count progress math as
    admin_broadcast_view's list -- just scoped to the ones actually active right now."""
    broadcasts = list(
        Broadcast.objects.filter(is_deleted=False, status__in=('running', 'paused'))
        .select_related('lead_list').annotate(lead_count=Count('lead_list__leads', distinct=True))
    )
    dialed_by_tag = {}
    for userfield in Cdr.objects.filter(userfield__startswith='Broadcast-').values_list('userfield', flat=True):
        prefix = userfield.split(':', 1)[0]
        dialed_by_tag[prefix] = dialed_by_tag.get(prefix, 0) + 1
    for b in broadcasts:
        b.dialed_count = dialed_by_tag.get(b.tag, 0)
        b.progress_percent = min(round(b.dialed_count * 100 / b.lead_count), 100) if b.lead_count else 0
    return broadcasts


def _active_auto_dials():
    """Auto Dial campaigns currently running or paused, with the same dialed/lead_count
    progress math as admin_auto_dial_view's list -- just scoped to the ones active right now."""
    campaigns = list(
        AutoDialCampaign.objects.filter(status__in=('running', 'paused'))
        .select_related('lead_list').annotate(lead_count=Count('lead_list__leads', distinct=True))
    )
    for c in campaigns:
        c.dialed_count = c.attempts.count()
        c.progress_percent = min(round(c.dialed_count * 100 / c.lead_count), 100) if c.lead_count else 0
    return campaigns


def _top_agents_today(limit=5):
    """Top agents today by queue calls handled -- same aggregation as the Performance report,
    just scoped to today and to real agent accounts (role='user')."""
    today = _local_today().isoformat()
    agent_names = set(
        Endpoint.objects.filter(assigned_user__role='user').values_list('assigned_user__full_name', flat=True)
    )
    logs = QueueLog.objects.exclude(queuename='NONE').filter(time__gte=today, time__lte=today + ' 23:59:59.999999')
    return [row for row in _agent_performance_rows(logs) if row['name'] in agent_names][:limit]


def _hourly_call_volume():
    """Today's calls (inbound + outbound), bucketed by hour of day -- powers the Dashboard's
    call volume chart."""
    today = _local_today()
    hourly = [0] * 24
    for start in _canonical_cdrs(Cdr.objects.filter(start__date=today)).values_list('start', flat=True):
        hourly[timezone.localtime(start).hour] += 1
    return hourly


def _recent_calls(limit=10):
    """Most recent calls (any direction, any day) for the Dashboard's live feed -- same
    src/dst agent-resolution rules as the CDR report (see admin_cdr_view), just the newest few."""
    endpoint_names = dict(
        Endpoint.objects.filter(assigned_user__isnull=False).values_list('id', 'assigned_user__full_name')
    )
    cdrs = _canonical_cdrs(Cdr.objects.exclude(userfield__startswith='Broadcast-')).order_by('-start')[:limit]
    rows = []
    for row in cdrs:
        if row.dcontext in INBOUND_TRUNK_CONTEXTS:
            answering_ext = _cdr_agent_endpoint_id(row)
            agent_name = endpoint_names.get(answering_ext) or answering_ext or '-'
            caller = row.src or 'Unknown'
        else:
            agent_name = endpoint_names.get(row.src) or row.src or '-'
            caller = row.dst or 'Unknown'
        duration = row.billsec or 0
        rows.append({
            'time': (
                f'{django_dateformat(timezone.localtime(row.start), DATE_FORMAT)} '
                f'{django_timeformat(timezone.localtime(row.start), TIME_FORMAT)}'
            ) if row.start else '-',
            'caller': caller,
            'agent': agent_name,
            'duration': f'{duration // 60:02d}:{duration % 60:02d}',
            'disposition': row.disposition or 'UNKNOWN',
        })
    return rows


@login_required
def admin_view(request):
    agents = _registered_agents_with_status()
    all_agent_endpoint_ids = list(Endpoint.objects.filter(assigned_user__role='user').values_list('id', flat=True))
    return render(request, 'admin/admin.html', {
        'active': 'dashboard',
        'total_login': len(agents),
        'total_incall': sum(1 for a in agents if a.status == 'On Call'),
        'agent_statuses': {a.id: a.status for a in agents},
        'live_queue_count': len(get_live_queue_callers()),
        'queue_breakdown': _live_queue_breakdown(),
        'server_stats': _server_stats(),
        'team_status': agents,
        'missed_calls_count': len(_recent_dropped_calls()),
        'paused_agents_count': sum(1 for a in agents if a.status == 'Paused'),
        'sla_percent': _today_sla(),
        **_today_queue_wait_stats(),
        'longest_wait': _longest_waiting_caller(),
        'active_broadcasts': _active_broadcasts(),
        'active_auto_dials': _active_auto_dials(),
        'top_agents': _top_agents_today(),
        'hourly_call_volume': _hourly_call_volume(),
        'recent_calls': _recent_calls(),
        **_today_call_stats(all_agent_endpoint_ids),
    })


def _local_today():
    """Today's date in the app's one fixed timezone (Asia/Dhaka -- see settings.TIME_ZONE).
    timezone.localtime(timezone.now()).date() would give the same answer here since there's no
    more per-request timezone activation to depend on; this just says so explicitly."""
    return timezone.localtime(timezone.now()).date()


@login_required
def admin_agent_performance_view(request):
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    # No range given -- default to today's data; older days are reachable via the date filter.
    if not date_from and not date_to:
        date_from = date_to = _local_today().isoformat()

    logs = QueueLog.objects.exclude(queuename='NONE')
    cdrs = Cdr.objects.filter(dcontext='from-internal')
    if date_from:
        logs = logs.filter(time__gte=date_from)
        try:
            cdrs = cdrs.filter(start__date__gte=datetime.date.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        logs = logs.filter(time__lte=date_to + ' 23:59:59.999999')
        try:
            cdrs = cdrs.filter(start__date__lte=datetime.date.fromisoformat(date_to))
        except ValueError:
            pass

    # Performance is agent-only -- admin/supervisor accounts shouldn't appear on the leaderboard
    # even if they technically have an extension. Scope both sources (Queue Log's membername and
    # CDR's src extension) to role='user' endpoints only.
    agent_endpoint_names = dict(
        Endpoint.objects.filter(assigned_user__role='user').values_list('id', 'assigned_user__full_name')
    )
    agent_names = set(agent_endpoint_names.values())
    ext_by_name = {name: ext for ext, name in agent_endpoint_names.items()}

    agent_rows = [row for row in _agent_performance_rows(logs) if row['name'] in agent_names]

    # Queue Log only covers inbound queue activity -- pull each agent's outbound count from CDR
    # too (src is their own extension on internal/outbound calls), keyed the same way
    # QueueMember.membername is assigned (assigned_user.full_name) so both sources line up on
    # one row per agent.
    outbound_counts = {}
    outbound_qs = _canonical_cdrs(cdrs).values('src').annotate(n=Count('id'))
    for row in outbound_qs:
        name = agent_endpoint_names.get(row['src'])
        if name:
            outbound_counts[name] = outbound_counts.get(name, 0) + row['n']

    rows_by_name = {row['name']: row for row in agent_rows}
    for name in outbound_counts:
        if name not in rows_by_name:
            new_row = {'name': name, 'calls': 0, 'missed': 0, 'avg_talk': 0, 'talk_total': 0, 'answer_rate': 0}
            agent_rows.append(new_row)
            rows_by_name[name] = new_row

    for row in agent_rows:
        row['inbound'] = row['calls']
        row['outbound'] = outbound_counts.get(row['name'], 0)
        row['calls'] = row['inbound'] + row['outbound']
        row['ext'] = ext_by_name.get(row['name'])
    agent_rows.sort(key=lambda r: r['calls'], reverse=True)

    def fmt_duration(seconds):
        seconds = int(seconds or 0)
        return f'{seconds // 60:02d}:{seconds % 60:02d}'

    for row in agent_rows:
        row['avg_talk_display'] = fmt_duration(row['avg_talk'])
        row['talk_total_display'] = fmt_duration(row['talk_total'])

    total_calls = sum(r['calls'] for r in agent_rows)
    total_missed = sum(r['missed'] for r in agent_rows)
    total_inbound = sum(r['inbound'] for r in agent_rows)
    total_outbound = sum(r['outbound'] for r in agent_rows)

    return render(request, 'admin/agent_performance.html', {
        'active': 'agent_performance', 'date_from': date_from, 'date_to': date_to,
        'agent_rows': agent_rows, 'total_calls': total_calls, 'total_missed': total_missed,
        'total_inbound': total_inbound, 'total_outbound': total_outbound,
        'agent_count': len(agent_rows),
    })


def _fmt_duration_hms(td):
    if not td:
        return '-'
    total_seconds = max(int(td.total_seconds()), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}'


@login_required
def admin_activity_view(request):
    """Reports -> Activity -- First Login/Last Logout/Total Login/Total Pause, one single day
    at a time (see _agent_activity_rows' docstring for why a date range doesn't make sense for
    these specific stats, unlike Performance's own call-count columns)."""
    today_local = _local_today()
    date_str = request.GET.get('date') or today_local.isoformat()
    try:
        day = datetime.date.fromisoformat(date_str)
    except ValueError:
        day = today_local
        date_str = day.isoformat()

    agent_endpoint_names = dict(
        Endpoint.objects.filter(assigned_user__role='user').values_list('id', 'assigned_user__full_name')
    )
    agent_names = set(agent_endpoint_names.values())
    time_fmt, date_fmt = TIME_FORMAT, DATE_FORMAT

    def fmt_datetime(dt):
        # Date included, not just time -- a session that spans midnight (login one day,
        # logout the next) would otherwise show an unlabeled time that looks like it belongs
        # to whichever day's report it's on, when it might actually be the next day's.
        return f'{django_dateformat(dt, date_fmt)} {django_timeformat(dt, time_fmt)}'

    activity_by_name = _agent_activity_rows(day)
    activity_rows = []
    for name, act in activity_by_name.items():
        if name not in agent_names:
            continue
        # No Activity that day at all (only Pause/Unpause activity, e.g. logged in on an
        # earlier day and never triggered a fresh dashboard login today) reads as "no data",
        # not "still active" -- see _agent_activity_rows.
        if act.get('first_login') is None:
            first_login_display = '-'
            last_logout_display = '-'
        else:
            first_login_display = fmt_datetime(act['first_login'])
            last_logout_display = 'Still Active' if act.get('still_open') else (
                fmt_datetime(act['last_logout']) if act.get('last_logout') else '-'
            )
        activity_rows.append({
            'name': name,
            'first_login': first_login_display,
            'last_logout': last_logout_display,
            'total_login': _fmt_duration_hms(act.get('total_login')),
            'total_pause': _fmt_duration_hms(act.get('total_pause')),
            # Ready = logged in and not paused -- the time actually available to take calls.
            # Zero-defaulted here (unlike the two columns above) purely so the subtraction has
            # real numbers to work with; a genuinely absent total_login still means 0 ready
            # time either way, so there's no separate "-" case worth preserving for this one.
            'total_ready': _fmt_duration_hms(max(
                (act.get('total_login') or datetime.timedelta()) - (act.get('total_pause') or datetime.timedelta()),
                datetime.timedelta(),
            )),
        })
    activity_rows.sort(key=lambda r: r['name'])

    selected_agent = request.GET.get('agent', '')
    timeline_data = _agent_activity_timeline(day, selected_agent) if selected_agent else []

    return render(request, 'admin/activity.html', {
        'active': 'activity', 'date': date_str, 'activity_rows': activity_rows,
        'selected_agent': selected_agent, 'timeline_data': timeline_data,
    })


def _auto_dial_cdr_parties(userfield):
    """Parses an Auto Dial CDR's userfield (AutoDial-<campaign_id>:<ext>:<number>, see
    ami.ami._dial_next_auto_dial_lead) into (ext, number), or None if it isn't one. Both src
    and dst can land wrong on this CDR row when the agent's own leg never gets answered (see
    that function's comment for why), so callers use this pair in preference to raw src/dst."""
    if not userfield or not userfield.startswith('AutoDial-'):
        return None
    parts = userfield.split(':')
    if len(parts) != 3:
        return None
    return parts[1], parts[2]


def _cdr_agent_endpoint_id(row):
    if row.dcontext == 'from-internal':
        return row.src
    match = re.match(r'PJSIP/(\w+)-', row.dstchannel or '')
    return match.group(1) if match else None


def _cdr_recording_path(row):
    """Finds the recording by searching for its uniqueid rather than reconstructing the exact
    filename agi/router.py or ami.ami wrote -- the filename also carries the call's date and
    queue/number for a human browsing the folder, but if Settings -> General's timezone changes
    after the fact, recomputing "the call's date" from the CDR row's stored UTC instant can
    land on a different day than what was actually written to disk. uniqueid never changes, so
    matching on `*_<uniqueid>.wav` is immune to that regardless of which date format or
    timezone was in effect when the file was created."""
    if not row.uniqueid:
        return None
    pattern = f'*_{row.uniqueid}.wav'

    if row.start:
        # Fast path: the call's current local date is right unless the timezone has changed
        # since it was recorded -- check that folder first before falling back to a full scan.
        local_start = timezone.localtime(row.start)
        guess_folder = os.path.join(RECORDINGS_DIR, local_start.strftime('%Y'), local_start.strftime('%m'), local_start.strftime('%d'))
        matches = glob.glob(os.path.join(guess_folder, pattern))
        if matches:
            return matches[0]

    matches = glob.glob(os.path.join(RECORDINGS_DIR, '**', pattern), recursive=True)
    return matches[0] if matches else None


@login_required
def admin_cdr_view(request):
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    search = request.GET.get('search', '').strip()
    disposition = request.GET.get('disposition', '')

    cdrs = _canonical_cdrs(Cdr.objects.exclude(userfield__startswith='Broadcast-'))
    if date_from:
        try:
            cdrs = cdrs.filter(start__date__gte=datetime.date.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            cdrs = cdrs.filter(start__date__lte=datetime.date.fromisoformat(date_to))
        except ValueError:
            pass
    if search:
        cdrs = cdrs.filter(Q(src__icontains=search) | Q(dst__icontains=search))
    if disposition:
        # Reports' drill-down links pass combined categories (e.g. "NO ANSWER,BUSY") since its
        # own outcome breakdown groups several raw dispositions into one card.
        cdrs = cdrs.filter(disposition__in=[d.strip() for d in disposition.split(',') if d.strip()])
    cdrs = cdrs.order_by('-start')

    paginator = Paginator(cdrs, 25)
    calls = paginator.get_page(request.GET.get('page'))

    endpoint_names = dict(
        Endpoint.objects.filter(assigned_user__isnull=False).values_list('id', 'assigned_user__full_name')
    )

    for row in calls:
        auto_dial_parties = _auto_dial_cdr_parties(row.userfield)

        if auto_dial_parties:
            # Preferred over raw src/dst unconditionally, not just when they look wrong -- see
            # _auto_dial_cdr_parties. Correct already-answered rows just get overwritten with
            # the same value; the ones broken by a never-answered agent leg get fixed.
            row.src_ext = auto_dial_parties[0]
            row.src_name = endpoint_names.get(row.src_ext)
        else:
            row.src_ext = row.src
            row.src_name = endpoint_names.get(row.src)

        # Inbound queue/IVR calls' dst is the DID (Queue()/run_ivr() never rewrite the dialplan
        # exten) -- the extension that actually answered lives in dstchannel instead.
        answering_ext = _cdr_agent_endpoint_id(row) if row.dcontext in INBOUND_TRUNK_CONTEXTS else None
        if answering_ext and answering_ext in endpoint_names:
            row.dst_ext = answering_ext
            row.dst_name = endpoint_names[answering_ext]
        elif auto_dial_parties:
            row.dst_ext = auto_dial_parties[1]
            row.dst_name = endpoint_names.get(row.dst_ext)
        else:
            row.dst_ext = row.dst
            row.dst_name = endpoint_names.get(row.dst)
        row.sequence = None

        recording_path = _cdr_recording_path(row)
        row.has_recording = bool(recording_path and os.path.isfile(recording_path))

    return render(request, 'admin/cdr.html', {
        'active': 'cdr', 'calls': calls,
        'filters': {'date_from': date_from, 'date_to': date_to, 'search': search, 'disposition': disposition},
    })


@login_required
def admin_cdr_recording_view(request, uniqueid):
    row = Cdr.objects.filter(uniqueid=uniqueid).order_by('-duration').first()
    if not row:
        raise Http404
    path = _cdr_recording_path(row)
    if not path or not os.path.isfile(path):
        raise Http404
    return FileResponse(open(path, 'rb'), content_type='audio/wav')


def _cel_endpoint_label(names, number):
    if not number:
        return None, number
    name = names.get(number)
    return (name, number) if name else (None, number)


@login_required
def admin_cel_view(request):
    search_query = request.GET.get('search', '').strip()
    selected_event_type = request.GET.get('event_type', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    selected_uniqueid = request.GET.get('uniqueid', '').strip()

    events = Cel.objects.all()
    if search_query:
        events = events.filter(
            Q(channame__icontains=search_query) | Q(cid_name__icontains=search_query)
            | Q(cid_num__icontains=search_query) | Q(exten__icontains=search_query)
        )
    if selected_event_type:
        events = events.filter(eventtype=selected_event_type)
    if date_from:
        try:
            events = events.filter(eventtime__gte=datetime.datetime.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            events = events.filter(eventtime__lte=datetime.datetime.fromisoformat(date_to))
        except ValueError:
            pass
    if selected_uniqueid:
        events = events.filter(Q(uniqueid=selected_uniqueid) | Q(linkedid=selected_uniqueid))
    events = events.order_by('-eventtime', '-id')

    event_stats = list(events.exclude(eventtype__isnull=True).values('eventtype').annotate(count=Count('id')).order_by('-count'))

    timeline_data = []
    if selected_uniqueid:
        time_fmt = TIME_FORMAT
        timeline_events = Cel.objects.filter(
            Q(uniqueid=selected_uniqueid) | Q(linkedid=selected_uniqueid)
        ).order_by('eventtime', 'id')
        for row in timeline_events:
            caller = row.cid_name or row.cid_num or row.channame or ''
            timeline_data.append({
                'type': row.eventtype,
                'time': django_timeformat(timezone.localtime(row.eventtime), time_fmt) if row.eventtime else '',
                'caller': caller,
            })

    paginator = Paginator(events, 50)
    page_obj = paginator.get_page(request.GET.get('page'))

    endpoint_names = dict(
        Endpoint.objects.filter(assigned_user__isnull=False).values_list('id', 'assigned_user__full_name')
    )
    for row in page_obj:
        row.caller_name, row.caller_ext = _cel_endpoint_label(endpoint_names, row.cid_num)
        row.exten_name, row.exten_ext = _cel_endpoint_label(endpoint_names, row.exten)

    return render(request, 'admin/cel.html', {
        'active': 'cel', 'page_obj': page_obj,
        'search_query': search_query, 'selected_event_type': selected_event_type,
        'date_from': date_from, 'date_to': date_to, 'selected_uniqueid': selected_uniqueid,
        'event_stats': event_stats, 'timeline_data': timeline_data,
        'event_types': Cel.objects.exclude(eventtype__isnull=True).order_by('eventtype').values_list('eventtype', flat=True).distinct(),
    })


# queue_log's data1-data5 mean different things per event type (Asterisk's own layout, not
# something we chose) -- maps event -> {field index: label} so the Details accordion can show
# "Waited: 5s" instead of a bare, unexplained "5".
_QUEUE_LOG_FIELD_LABELS = {
    'ENTERQUEUE': {2: 'Caller ID', 3: 'Position'},
    'ABANDON': {1: 'Position', 2: 'Orig. Position', 3: 'Waited'},
    'COMPLETEAGENT': {1: 'Waited', 2: 'Talked', 3: 'Completed By'},
    'COMPLETECALLER': {1: 'Waited', 2: 'Talked', 3: 'Completed By'},
    'CONNECT': {1: 'Waited', 3: 'Ring Time'},
    'RINGNOANSWER': {1: 'Rang'},
    'RINGCANCELED': {1: 'Ring Time'},
    'PAUSE': {1: 'Reason'},
    'TRANSFER': {1: 'To Exten', 2: 'To Context', 3: 'Waited', 4: 'Talked'},
}
_QUEUE_LOG_SECONDS_LABELS = {'Waited', 'Talked', 'Rang'}


def _queue_log_time_display(raw, with_date=False):
    """QueueLog.time is a raw string Asterisk writes directly (realtime, not a DateTimeField) --
    format it to match the app's fixed date/time format, same as everywhere else (CDR, CEL, ...)."""
    if not raw:
        return ''
    try:
        dt = datetime.datetime.strptime(raw.split('.')[0], '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return raw
    if with_date:
        return f'{django_dateformat(dt, DATE_FORMAT)} {django_timeformat(dt, TIME_FORMAT)}'
    return django_timeformat(dt, TIME_FORMAT)


def _queue_log_field_display(event, index, raw_value):
    if not raw_value:
        return '-'
    label = _QUEUE_LOG_FIELD_LABELS.get(event, {}).get(index)
    if not label:
        return raw_value
    if label in _QUEUE_LOG_SECONDS_LABELS:
        return f'{label}: {raw_value}s'
    if label == 'Ring Time':
        return f'{label}: {raw_value}ms'
    return f'{label}: {raw_value}'


def _queue_log_resolve_agent(endpoint_names, raw):
    """agent carries the QueueMember's membername (or interface) -- resolve to the assigned
    user's full name when it happens to match a real extension id."""
    if not raw or raw == 'NONE':
        return None, None
    return endpoint_names.get(raw), raw


@login_required
def admin_queue_log_view(request):
    search_query = request.GET.get('search', '').strip()
    selected_event = request.GET.get('event', '')
    selected_queue = request.GET.get('queue', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    # Non-call administrative events (CONFIGRELOAD, PAUSE, ...) carry callid='NONE' -- this page
    # is one row per call, so only rows tied to a real call qualify.
    base_logs = QueueLog.objects.exclude(queuename='NONE').exclude(callid__isnull=True).exclude(callid='').exclude(callid='NONE')
    if date_from:
        base_logs = base_logs.filter(time__gte=date_from)
    if date_to:
        base_logs = base_logs.filter(time__lte=date_to + ' 23:59:59.999999')

    qualifying = base_logs
    if search_query:
        qualifying = qualifying.filter(Q(agent__icontains=search_query) | Q(callid__icontains=search_query) | Q(queuename__icontains=search_query))
    if selected_queue:
        qualifying = qualifying.filter(queuename=selected_queue)
    if selected_event:
        qualifying = qualifying.filter(event=selected_event)

    event_stats = list(qualifying.exclude(event__isnull=True).values('event').annotate(count=Count('id')).order_by('-count'))

    callids_ordered = list(
        qualifying.values('callid').annotate(last_time=Max('time')).order_by('-last_time').values_list('callid', flat=True)
    )
    total_count = len(callids_ordered)

    paginator = Paginator(callids_ordered, 25)
    page_obj = paginator.get_page(request.GET.get('page'))
    page_callids = list(page_obj)

    all_rows = list(QueueLog.objects.filter(callid__in=page_callids).order_by('time', 'id'))
    grouped = {}
    for row in all_rows:
        grouped.setdefault(row.callid, []).append(row)

    endpoint_names = dict(
        Endpoint.objects.filter(assigned_user__isnull=False).values_list('id', 'assigned_user__full_name')
    )

    calls = []
    for callid in page_callids:
        events = grouped.get(callid, [])
        if not events:
            continue
        entry_event = next((e for e in events if e.event == 'ENTERQUEUE'), events[0])
        final_event_row = events[-1]

        wait_time, agent_raw = None, None
        for e in events:
            if e.event == 'ABANDON':
                wait_time = _report_int(e.data3)
            elif e.event in ('COMPLETEAGENT', 'COMPLETECALLER', 'CONNECT'):
                wait_time = _report_int(e.data1)
                agent_raw = e.agent
            elif e.event == 'RINGNOANSWER' and agent_raw is None:
                agent_raw = e.agent
        if agent_raw is None:
            agent_raw = next((e.agent for e in events if e.agent and e.agent != 'NONE'), None)
        agent_name, agent_ext = _queue_log_resolve_agent(endpoint_names, agent_raw)

        sub_events = []
        for e in events:
            e_agent_name, e_agent_ext = _queue_log_resolve_agent(endpoint_names, e.agent)
            sub_events.append({
                'local_time': _queue_log_time_display(e.time),
                'event': e.event, 'agent_name': e_agent_name, 'agent_ext': e_agent_ext or '-',
                'data1_display': _queue_log_field_display(e.event, 1, e.data1),
                'data2_display': _queue_log_field_display(e.event, 2, e.data2),
                'data3_display': _queue_log_field_display(e.event, 3, e.data3),
                'data4_display': _queue_log_field_display(e.event, 4, e.data4),
                'data5_display': _queue_log_field_display(e.event, 5, e.data5),
            })

        calls.append({
            'entry_time': _queue_log_time_display(entry_event.time, with_date=True), 'queue_name': events[0].queuename, 'final_event': final_event_row.event,
            'agent_name': agent_name, 'agent_ext': agent_ext or '-',
            'wait_time': f'{wait_time}s' if wait_time is not None else '-',
            'callid': callid, 'event_count': len(events), 'events': sub_events,
        })

    return render(request, 'admin/queue_log.html', {
        'active': 'queue_log', 'calls': calls, 'page_obj': page_obj, 'total_count': total_count,
        'event_stats': event_stats,
        'search_query': search_query, 'selected_event': selected_event, 'selected_queue': selected_queue,
        'date_from': date_from, 'date_to': date_to,
        'event_types': QueueLog.objects.exclude(event__isnull=True).order_by('event').values_list('event', flat=True).distinct(),
        'queue_names': QueueLog.objects.exclude(queuename='NONE').order_by('queuename').values_list('queuename', flat=True).distinct(),
    })


@login_required
def admin_auto_dial_view(request):
    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'create':
            name = request.POST.get('name', '').strip()
            lead_list_id = request.POST.get('lead_list')
            queue_name = request.POST.get('queue')
            if not name or not lead_list_id or not queue_name:
                messages.error(request, 'Name, Lead List and Queue are all required.')
                return redirect('admin_auto_dial')
            AutoDialCampaign.objects.create(
                name=name, lead_list_id=lead_list_id, queue_name=queue_name, created_by=request.user,
            )
            messages.success(request, f'"{name}" created.')
            return redirect('admin_auto_dial')

        if action == 'edit':
            campaign = get_object_or_404(AutoDialCampaign, id=request.POST.get('id'))
            if campaign.status == 'running':
                messages.error(request, 'Pause the campaign before editing it.')
                return redirect('admin_auto_dial')
            name = request.POST.get('name', '').strip()
            if not name:
                messages.error(request, 'Enter a name for the campaign.')
                return redirect('admin_auto_dial')
            campaign.name = name
            campaign.lead_list_id = request.POST.get('lead_list')
            campaign.queue_name = request.POST.get('queue')
            campaign.save()
            messages.success(request, f'"{name}" updated.')
            return redirect('admin_auto_dial')

        if action == 'delete':
            target = AutoDialCampaign.objects.filter(id=request.POST.get('id')).first()
            if target and target.status == 'running':
                # Same reasoning as Broadcast's delete guard: the dialing hook (see
                # ami.ami._auto_dial_campaign_by_queue) only holds the campaign_id and queue
                # name in memory -- deleting the row out from under a running campaign wouldn't
                # stop it dialing agents, it'd just orphan those dials with no row to show for
                # them and no way to ever mark the campaign completed.
                messages.error(request, 'Pause the campaign before deleting it.')
                return redirect('admin_auto_dial')
            AutoDialCampaign.objects.filter(id=request.POST.get('id')).delete()
            messages.success(request, 'Campaign removed.')
            return redirect('admin_auto_dial')

    campaigns = list(AutoDialCampaign.objects.select_related('lead_list').annotate(
        lead_count=Count('lead_list__leads', distinct=True),
    ))
    for campaign in campaigns:
        campaign.dialed_count = campaign.attempts.count()
        campaign.connected_count = campaign.attempts.filter(status='connected').count()
        campaign.progress_percent = (
            min(round(campaign.dialed_count * 100 / campaign.lead_count), 100) if campaign.lead_count else 0
        )
    return render(request, 'admin/auto_dial.html', {
        'active': 'auto_dial', 'campaigns': campaigns,
        'lead_lists': LeadList.objects.all(),
        'queues': Queue.objects.filter(is_active=True),
    })


@login_required
async def admin_auto_dial_start_view(request, campaign_id):
    """Must be async, same reason as admin_broadcast_start_view: it talks to the same
    panoramisk Manager the AMI listener owns, which isn't thread/loop-safe."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    from ami.ami import start_auto_dial
    started = await start_auto_dial(campaign_id)
    if not started:
        messages.error(request, 'Another campaign is already running against that queue -- pause it first.')
    return redirect('admin_auto_dial')


@login_required
async def admin_auto_dial_pause_view(request, campaign_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    from ami.ami import pause_auto_dial
    await pause_auto_dial(campaign_id)
    return redirect('admin_auto_dial')


def _parse_broadcast_schedule(request):
    """scheduled_at/scheduled_end_at -- the optional auto start/stop window (see
    ami.ami._start_due_scheduled_broadcasts/_stop_due_scheduled_broadcasts). Shared between the
    create and edit POST branches below. Returns (extras_dict, error_message); error_message is
    None on success.

    The <input type=datetime-local> submits a naive wall-clock string with no timezone info,
    meant in the app's one fixed timezone (Asia/Dhaka -- see settings.TIME_ZONE).
    timezone.make_aware() with no explicit tz argument uses exactly that. Skipping this and
    saving the naive value directly would let Django's own naive-datetime-on-save fallback
    (DateTimeField.get_prep_value) silently assume UTC instead."""
    scheduled_at = None
    scheduled_at_raw = request.POST.get('scheduled_at', '').strip()
    if scheduled_at_raw:
        try:
            scheduled_at = timezone.make_aware(datetime.datetime.fromisoformat(scheduled_at_raw))
        except ValueError:
            return None, 'Invalid Schedule Start date/time.'

    scheduled_end_at = None
    scheduled_end_at_raw = request.POST.get('scheduled_end_at', '').strip()
    if scheduled_end_at_raw:
        try:
            scheduled_end_at = timezone.make_aware(datetime.datetime.fromisoformat(scheduled_end_at_raw))
        except ValueError:
            return None, 'Invalid Schedule End date/time.'

    if scheduled_at and scheduled_end_at and scheduled_end_at <= scheduled_at:
        return None, 'Schedule End must be after Schedule Start.'

    return {'scheduled_at': scheduled_at, 'scheduled_end_at': scheduled_end_at}, None


def _broadcast_digit_option_errors(formset):
    return [
        e[0] if field == '__all__' else f'Key {f.cleaned_data.get("digit") or "?"}: {e[0]}'
        for f in formset for field, e in f.errors.items()
    ]


@login_required
def admin_broadcast_view(request):
    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'delete':
            target = Broadcast.objects.filter(id=request.POST.get('id')).first()
            if target and target.status == 'running':
                # The background dialing loop (ami.ami._run_broadcast) only holds broadcast_id
                # and keeps dialing off its own in-memory state -- hiding the row out from under
                # it wouldn't stop a single real call, just make an active campaign vanish from
                # the list while it's still dialing. Pause first (which the loop does check) so
                # nothing's still running when it goes away.
                messages.error(request, 'Pause the broadcast before deleting it.')
                return redirect('admin_broadcast')
            # Soft delete only -- never a real .delete(). The row (and every Cdr row already
            # tagged with its .tag) stays intact forever, just hidden from this list, so
            # Reports -> Broadcast can still show its history by name after this.
            Broadcast.objects.filter(id=request.POST.get('id')).update(is_deleted=True)
            messages.success(request, 'Broadcast removed.')
            return redirect('admin_broadcast')

    broadcasts = list(Broadcast.objects.filter(is_deleted=False).select_related('lead_list', 'sound', 'trunk').annotate(
        lead_count=Count('lead_list__leads', distinct=True)
    ))

    # One query instead of one COUNT per broadcast row. userfield is 'tag' (older rows, from
    # before the dialed number was encoded into it) or 'tag:number' (current -- see
    # ami.ami._dial_one) -- grouping by the part before ':' covers both formats so a call
    # nobody answered still counts as dialed even though Asterisk attributes its CDR to the
    # trunk's inbound context (dst='s') instead of our own [broadcast] context in that case.
    dialed_by_tag = {}
    for userfield in Cdr.objects.filter(userfield__startswith='Broadcast-').values_list('userfield', flat=True):
        prefix = userfield.split(':', 1)[0]
        dialed_by_tag[prefix] = dialed_by_tag.get(prefix, 0) + 1
    for broadcast in broadcasts:
        broadcast.dialed_count = dialed_by_tag.get(broadcast.tag, 0)
        broadcast.progress_percent = (
            min(round(broadcast.dialed_count * 100 / broadcast.lead_count), 100) if broadcast.lead_count else 0
        )
    return render(request, 'admin/broadcast.html', {
        'active': 'broadcast', 'broadcasts': broadcasts,
    })


@login_required
def admin_broadcast_form_view(request, broadcast_id=None):
    """Create (broadcast_id is None) and Edit share this one page/view -- an unsaved Broadcast()
    for create, a real instance for edit; the digit-options formset and every field render the
    same way either way, Django's own instance binding handles the difference."""
    broadcast = get_object_or_404(Broadcast, id=broadcast_id) if broadcast_id else None
    if broadcast and broadcast.status == 'running':
        messages.error(request, 'Pause the broadcast before editing it.')
        return redirect('admin_broadcast')

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        lead_list_id = request.POST.get('lead_list')
        sound_id = request.POST.get('sound')
        trunk_id = request.POST.get('trunk')
        concurrent_calls = request.POST.get('concurrent_calls') or 5
        if not name or not lead_list_id or not sound_id or not trunk_id:
            messages.error(request, 'Name, Lead List, Sound and Trunk are all required.')
            return redirect(request.path)
        extras, error = _parse_broadcast_schedule(request)
        if error:
            messages.error(request, error)
            return redirect(request.path)

        # Unsaved on purpose (for create) -- the formset below validates against this instance
        # (its FK rows don't need broadcast.pk to exist yet, only to *save*), so nothing is
        # committed until both the broadcast and its key mappings are known-valid together.
        instance = broadcast or Broadcast(created_by=request.user)
        instance.name = name
        instance.lead_list_id = lead_list_id
        instance.sound_id = sound_id
        instance.trunk_id = trunk_id
        instance.concurrent_calls = concurrent_calls
        instance.scheduled_at = extras['scheduled_at']
        instance.scheduled_end_at = extras['scheduled_end_at']

        formset = BroadcastDigitOptionFormSet(request.POST, instance=instance, prefix='options')
        if formset.is_valid():
            instance.save()
            formset.save()
            messages.success(request, f'"{name}" {"updated" if broadcast else "created"}.')
            return redirect('admin_broadcast')
        messages.error(request, ' '.join(_broadcast_digit_option_errors(formset)) or 'Please correct the key mapping errors.')
        return redirect(request.path)

    return render(request, 'admin/broadcast_form.html', {
        'active': 'broadcast', 'broadcast': broadcast,
        'lead_lists': LeadList.objects.all(),
        'sounds': Sound.objects.all(),
        'trunks': Endpoint.objects.filter(assigned_user__isnull=True),
        'queues': Queue.objects.filter(is_active=True),
        'ivrs': Ivr.objects.filter(is_active=True),
        'extensions': Endpoint.objects.filter(assigned_user__role='user').order_by('id'),
        # localtime() first -- these are stored UTC-aware, and the <input type=datetime-local>
        # this feeds needs the admin's own wall-clock time back (see _parse_broadcast_schedule),
        # not the raw UTC value.
        'scheduled_at_local': timezone.localtime(broadcast.scheduled_at).strftime('%Y-%m-%dT%H:%M') if broadcast and broadcast.scheduled_at else '',
        'scheduled_end_at_local': timezone.localtime(broadcast.scheduled_end_at).strftime('%Y-%m-%dT%H:%M') if broadcast and broadcast.scheduled_end_at else '',
        'formset': BroadcastDigitOptionFormSet(instance=broadcast or Broadcast(), prefix='options'),
    })


@login_required
def admin_broadcast_detail_view(request, broadcast_id):
    broadcast = get_object_or_404(Broadcast, id=broadcast_id)

    # userfield is 'tag' (older rows) or 'tag:number' (current -- see ami.ami._dial_one),
    # matched without requiring dcontext='broadcast' -- a call nobody answers gets its CDR
    # attributed to the trunk's inbound context instead (dst='s'), so restricting to our own
    # context used to make those calls silently disappear from every stat below instead of
    # counting as No Answer, which is what they actually are.
    calls = list(
        Cdr.objects.filter(Q(userfield=broadcast.tag) | Q(userfield__startswith=f'{broadcast.tag}:'))
        .order_by('-start')
    )
    for call in calls:
        call.real_number = call.userfield.split(':', 1)[1] if ':' in call.userfield else call.dst

    total_leads = broadcast.lead_list.leads.count()
    answered = sum(1 for c in calls if c.disposition == 'ANSWERED')
    no_answer = sum(1 for c in calls if c.disposition == 'NO ANSWER')
    failed = sum(1 for c in calls if c.disposition in ('BUSY', 'FAILED', 'CONGESTION'))

    return render(request, 'admin/broadcast_detail.html', {
        'active': 'broadcast', 'broadcast': broadcast, 'calls': calls,
        'total_leads': total_leads, 'dialed': len(calls),
        'answered': answered, 'no_answer': no_answer, 'failed': failed,
    })


@login_required
def admin_broadcast_report_view(request):
    """Reports -> Broadcast -- every broadcast call across every campaign, one combined table
    (Settings -> Broadcast's own per-campaign detail page stays as the quick "just this one"
    view). Reads every Broadcast ever created, including soft-deleted ones (see
    Broadcast.is_deleted / admin_broadcast_view's delete action) -- deleting a campaign only
    hides it from that list, never its name or its Cdr history, so this page is the one place
    that history is still reachable afterwards."""
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    selected_broadcast_id = request.GET.get('broadcast', '')

    all_broadcasts = list(Broadcast.objects.order_by('-created_at').only('id', 'name', 'created_at'))
    name_by_tag = {b.tag: b.name for b in all_broadcasts}

    cdrs = _canonical_cdrs(Cdr.objects.filter(userfield__startswith='Broadcast-'))
    if selected_broadcast_id:
        selected = next((b for b in all_broadcasts if str(b.id) == selected_broadcast_id), None)
        if selected:
            cdrs = cdrs.filter(Q(userfield=selected.tag) | Q(userfield__startswith=f'{selected.tag}:'))
    if date_from:
        try:
            cdrs = cdrs.filter(start__date__gte=datetime.date.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            cdrs = cdrs.filter(start__date__lte=datetime.date.fromisoformat(date_to))
        except ValueError:
            pass
    cdrs = cdrs.order_by('-start')

    paginator = Paginator(cdrs, 25)
    calls = paginator.get_page(request.GET.get('page'))

    for row in calls:
        tag = row.userfield.split(':', 1)[0] if row.userfield else ''
        row.broadcast_id = tag
        row.broadcast_name = name_by_tag.get(tag, '')
        row.real_number = row.userfield.split(':', 1)[1] if row.userfield and ':' in row.userfield else row.dst

    return render(request, 'admin/broadcast_report.html', {
        'active': 'broadcast_report', 'calls': calls, 'broadcasts': all_broadcasts,
        'filters': {'date_from': date_from, 'date_to': date_to, 'broadcast': selected_broadcast_id},
    })


@login_required
async def admin_broadcast_start_view(request, broadcast_id):
    """Must be async, same reason as user_toggle_pause_view: the dialing loop it kicks off
    (see ami.ami.start_broadcast) talks to the same panoramisk Manager the AMI listener owns,
    which isn't thread/loop-safe -- has to run on that one event loop."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    from ami.ami import start_broadcast
    started = await start_broadcast(broadcast_id)
    if not started:
        messages.info(request, 'The previous run is still finishing its last call -- try again in a moment.')
    return redirect('admin_broadcast')


@login_required
async def admin_broadcast_pause_view(request, broadcast_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    from ami.ami import pause_broadcast
    await pause_broadcast(broadcast_id)
    return redirect('admin_broadcast')


def _ivr_destination_label(dtype, did):
    if dtype == 'extension':
        endpoint = Endpoint.objects.filter(id=did).first()
        name = endpoint.assigned_user.full_name if endpoint and endpoint.assigned_user else None
        return f'Ext {did}' + (f' — {name}' if name else '')
    if dtype == 'queue':
        return f'Group — {did}'
    if dtype == 'ivr':
        return f'IVR — {did}'
    return did


@login_required
def admin_settings_ivr_view(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        ivr_id = request.POST.get('id') or None
        if action == 'delete':
            ivr = Ivr.objects.filter(id=ivr_id).first() if ivr_id else None
            if ivr:
                Route.objects.filter(direction='inbound', destination_type='ivr', destination_id=ivr.name).update(destination_type='', destination_id='')
                ivr.delete()
                messages.success(request, 'IVR menu deleted.')
        else:
            instance = Ivr.objects.filter(id=ivr_id).first() if ivr_id else None
            form = IvrForm(request.POST, instance=instance)
            if form.is_valid():
                ivr = form.save(commit=False)
                formset = IvrOptionFormSet(request.POST, instance=ivr, prefix='options', form_kwargs={'ivr_name': form.cleaned_data['name']})
                if formset.is_valid():
                    ivr.save()
                    formset.save()
                    messages.success(request, 'IVR menu saved.')
                else:
                    errors = [
                        e[0] if field == '__all__' else f'Key {f.cleaned_data.get("digit") or "?"}: {e[0]}'
                        for f in formset for field, e in f.errors.items()
                    ]
                    messages.error(request, ' '.join(errors) or 'Please correct the key mapping errors.')
            else:
                messages.error(request, ' '.join(f'{f}: {e[0]}' for f, e in form.errors.items()))
        return redirect('admin_settings_ivr')

    routes_by_ivr = {}
    for route in Route.objects.filter(direction='inbound', destination_type='ivr', is_active=True):
        routes_by_ivr.setdefault(route.destination_id, []).append(route.did)

    menus = []
    for ivr in Ivr.objects.order_by('name'):
        options = list(ivr.options.all())
        keys = [{'digit': opt.digit, 'label': _ivr_destination_label(opt.destination_type, opt.destination_id)} for opt in options]
        edit_payload = {
            'id': ivr.id, 'name': ivr.name, 'description': ivr.description,
            'greeting_sound': ivr.greeting_sound_id or '', 'invalid_sound': ivr.invalid_sound_id or '',
            'timeout': ivr.timeout, 'max_retries': ivr.max_retries, 'is_active': ivr.is_active,
            'options': [{'digit': opt.digit, 'destination_type': opt.destination_type, 'destination_id': opt.destination_id} for opt in options],
        }
        menus.append({
            'id': ivr.id, 'name': ivr.name, 'description': ivr.description,
            'greeting_sound': ivr.greeting_sound, 'is_active': ivr.is_active,
            'keys': keys, 'dids': routes_by_ivr.get(ivr.name, []),
            'edit_payload': edit_payload, 'dom_id': f'ivr-data-{ivr.id}',
        })

    form = IvrForm()
    formset = IvrOptionFormSet(instance=Ivr(), prefix='options')
    return render(request, 'admin/ivr.html', {
        'active': 'settings', 'settings_active': 'ivr',
        'form': form, 'formset': formset, 'menus': menus,
        'extensions': Endpoint.objects.filter(assigned_user__role='user').order_by('id'),
        'queues': Queue.objects.exclude(name__endswith='-opus').order_by('name'),
        'ivrs': Ivr.objects.order_by('name'),
    })


def _crm_list_context(request):
    """Read-only CRM list/search/stats -- shared by admin (monitor-only) and the agent-facing
    CRM tab (full CRUD; agents fill this in, admin just watches)."""
    search = request.GET.get('search', '').strip()
    tag_filter = request.GET.get('tag', '')

    contacts = Crm.objects.select_related('owner').all()
    if search:
        contacts = contacts.filter(Q(name__icontains=search) | Q(phone__icontains=search) | Q(email__icontains=search))
    if tag_filter:
        contacts = contacts.filter(tag=tag_filter)
    contacts = contacts.order_by('-created_at')

    paginator = Paginator(contacts, 25)
    page_obj = paginator.get_page(request.GET.get('page'))

    # "Last Contact" / call count aren't stored -- looked up live from Cdr by phone match, so
    # they can never drift out of sync with the actual call history.
    phones = [c.phone for c in page_obj]
    cdr_agg = {}
    if phones:
        for row in _canonical_cdrs(Cdr.objects.filter(Q(src__in=phones) | Q(dst__in=phones))):
            for phone in (row.src, row.dst):
                if phone in phones:
                    entry = cdr_agg.setdefault(phone, {'count': 0, 'last': None})
                    entry['count'] += 1
                    if entry['last'] is None or (row.start and row.start > entry['last']):
                        entry['last'] = row.start

    owner_exts = dict(
        Endpoint.objects.filter(assigned_user__isnull=False).values_list('assigned_user_id', 'id')
    )

    rows = []
    for contact in page_obj:
        agg = cdr_agg.get(contact.phone, {'count': 0, 'last': None})
        edit_payload = {
            'id': contact.id, 'name': contact.name, 'phone': contact.phone, 'email': contact.email,
            'tag': contact.tag, 'owner_id': contact.owner_id,
            'follow_up_date': contact.follow_up_date.isoformat() if contact.follow_up_date else '',
            'notes': contact.notes,
        }
        rows.append({
            'contact': contact, 'call_count': agg['count'], 'last_contact': agg['last'],
            'owner_ext': owner_exts.get(contact.owner_id),
            'edit_payload': edit_payload, 'dom_id': f'crm-data-{contact.id}',
        })

    querystring = request.GET.copy()
    querystring.pop('page', None)

    return {
        'rows': rows, 'page_obj': page_obj, 'form': CrmForm(),
        'search': search, 'tag_filter': tag_filter, 'querystring': querystring.urlencode(),
    }


@login_required
def admin_crm_view(request):
    return render(request, 'admin/crm.html', {'active': 'crm', **_crm_list_context(request)})


def _crm_handle_post(request):
    """Create/edit/delete a contact -- agent-facing only (see _crm_list_context's docstring).
    Owner is always forced to the logged-in agent, never taken from the submitted form."""
    action = request.POST.get('action')
    contact_id = request.POST.get('id') or None
    if action == 'delete':
        contact = Crm.objects.filter(id=contact_id).first() if contact_id else None
        if contact:
            contact.delete()
            messages.success(request, 'Contact deleted.')
        return
    instance = Crm.objects.filter(id=contact_id).first() if contact_id else None
    form = CrmForm(request.POST, instance=instance)
    if form.is_valid():
        contact = form.save(commit=False)
        contact.owner = request.user
        contact.save()
        messages.success(request, 'Contact saved.')
    else:
        messages.error(request, ' '.join(f'{f}: {e[0]}' for f, e in form.errors.items()))


@login_required
def logout_view(request):
    if request.user.is_authenticated:
        Activity.objects.filter(user=request.user, logout_at__isnull=True).update(logout_at=timezone.now())
    logout(request)
    messages.success(request, 'You have been logged out.')
    return redirect('/')


@login_required
def admin_settings_queue_view(request):
    if request.method == 'POST':
        form = QueueQuickCreateForm(request.POST)
        if form.is_valid():
            queue = form.save()
            _queue_reload()
            messages.success(request, f'Queue "{queue.name}" created — click it to configure its settings.')
            return redirect('admin_settings_queue')
        else:
            for field_errors in form.errors.values():
                for error in field_errors:
                    messages.error(request, error)
    else:
        form = QueueQuickCreateForm()

    # Excludes "<name>-opus" mirrors -- agi/router.py rebuilds one per WhatsApp call routed to
    # a queue (see route_inbound) so Queue() still respects that queue's real ring strategy
    # despite the opus-only codec restriction; they're a routing implementation detail, not
    # something an admin creates or manages directly.
    queues = Queue.objects.exclude(name__endswith='-opus').order_by('name')
    member_counts = {}
    for row in QueueMember.objects.values('queue_name').all():
        member_counts[row['queue_name']] = member_counts.get(row['queue_name'], 0) + 1

    queue_rows = []
    for q in queues:
        live = _get_queue_live_status(q.name)
        queue_rows.append({'queue': q, 'member_count': member_counts.get(q.name, 0), 'live': live})

    total_queues = len(queue_rows)
    active_queues = sum(1 for q in queues if q.is_active)

    return render(request, 'admin/queue.html', {
        'active': 'settings', 'settings_active': 'queue', 'queue_rows': queue_rows, 'form': form,
        'total_queues': total_queues, 'active_queues': active_queues, 'inactive_queues': total_queues - active_queues,
    })


@login_required
def admin_settings_queue_form_view(request, queue_name):
    queue = get_object_or_404(Queue, name=queue_name)

    if request.method == 'POST':
        form = QueueForm(request.POST, instance=queue)
        if form.is_valid():
            new_name = form.cleaned_data['name']
            # Captured from the URL, not queue.name — ModelForm validation mutates
            # form.instance.name (== queue.name) to the new value as a side effect of
            # is_valid(), so reading queue.name here would already show the new name.
            old_name = queue_name
            renamed = new_name != old_name
            if renamed:
                # Queue.name is the primary key — instance.save() with a changed pk would
                # INSERT a new row and leave the old one behind, rather than renaming it.
                # A plain UPDATE actually renames it; then cascade the rename to members and
                # any Route that references this Queue by name (destination_id/source_id are
                # plain strings, not a FK, since they're polymorphic across extension/queue/ivr).
                Queue.objects.filter(name=old_name).update(name=new_name)
                QueueMember.objects.filter(queue_name=old_name).update(queue_name=new_name)
                Route.objects.filter(destination_type='queue', destination_id=old_name).update(destination_id=new_name)
                Route.objects.filter(direction='outbound', source_type='queue', source_id=old_name).update(source_id=new_name)
                queue = Queue.objects.get(name=new_name)
                form = QueueForm(request.POST, instance=queue)
                form.is_valid()
            form.save()
            _queue_reload()
            messages.success(request, f'Group "{form.instance.name}" saved.')
            return redirect('admin_settings_queue_edit', queue_name=form.instance.name)
        else:
            for field_errors in form.errors.values():
                for error in field_errors:
                    messages.error(request, error)
    else:
        form = QueueForm(instance=queue)

    members = QueueMember.objects.filter(queue_name=queue.name).order_by('penalty', 'interface')
    member_penalties = {}
    for m in members:
        if '/' in m.interface:
            member_penalties[m.interface.split('/', 1)[1]] = m.penalty

    # An extension already belongs to at most one queue — enforced here because the outbound
    # membership check (REALTIME_FIELD on queue_members by interface) only ever sees one row
    # per interface, so being in two Groups at once would make that lookup ambiguous.
    taken_elsewhere = {}
    for m in QueueMember.objects.exclude(queue_name=queue.name):
        if '/' in m.interface:
            taken_elsewhere[m.interface.split('/', 1)[1]] = m.queue_name

    member_rows = []
    for ext in Endpoint.objects.filter(assigned_user__role='user').order_by('id'):
        name = ext.assigned_user.full_name if ext.assigned_user else None
        member_rows.append({
            'id': ext.id,
            'label': f'{ext.id} — {name}' if name else ext.id,
            'checked': ext.id in member_penalties,
            'penalty': member_penalties.get(ext.id, 0),
            'taken_by': taken_elsewhere.get(ext.id),
        })

    return render(request, 'admin/settings_queue_form.html', {
        'active': 'settings', 'settings_active': 'queue', 'form': form, 'editing': True,
        'queue': queue, 'member_rows': member_rows,
    })


@login_required
def admin_settings_queue_toggle_view(request, queue_name):
    if request.method == 'POST':
        queue = get_object_or_404(Queue, name=queue_name)
        queue.is_active = not queue.is_active
        queue.save(update_fields=['is_active'])
        messages.success(request, f"Queue \"{queue_name}\" is now {'active' if queue.is_active else 'inactive'}.")
    return redirect('admin_settings_queue')


@login_required
def admin_settings_queue_delete_view(request, queue_name):
    if request.method == 'POST':
        queue = get_object_or_404(Queue, name=queue_name)
        Route.objects.filter(destination_type='queue', destination_id=queue_name).delete()
        Route.objects.filter(direction='outbound', source_type='queue', source_id=queue_name).delete()
        QueueMember.objects.filter(queue_name=queue_name).delete()
        queue.delete()
        _queue_reload()
        messages.success(request, f'Queue "{queue_name}" deleted.')
    return redirect('admin_settings_queue')


@login_required
def admin_settings_queue_members_update_view(request, queue_name):
    if request.method == 'POST':
        queue = get_object_or_404(Queue, name=queue_name)
        form = QueueMembersForm(request.POST)
        if form.is_valid():
            selected = form.cleaned_data['members']

            # An extension can only belong to one queue at a time — see the matching comment in
            # admin_settings_queue_form_view for why (ambiguous REALTIME_FIELD lookup otherwise).
            taken_elsewhere = {}
            for m in QueueMember.objects.exclude(queue_name=queue_name):
                if '/' in m.interface:
                    taken_elsewhere[m.interface.split('/', 1)[1]] = m.queue_name
            blocked = [ext for ext in selected if ext.id in taken_elsewhere]
            if blocked:
                for ext in blocked:
                    messages.error(request, f'{ext.id} is already a member of "{taken_elsewhere[ext.id]}" — remove them there first.')
                selected = [ext for ext in selected if ext.id not in taken_elsewhere]

            selected_ids = {ext.id for ext in selected}

            current = list(QueueMember.objects.filter(queue_name=queue_name))
            current_by_id = {m.interface.split('/', 1)[1]: m for m in current if '/' in m.interface}
            current_ids = set(current_by_id.keys())

            to_remove_ids = current_ids - selected_ids
            for ext_id in to_remove_ids:
                current_by_id[ext_id].delete()

            changed = bool(to_remove_ids)
            for ext in selected:
                penalty_raw = request.POST.get(f'penalty_{ext.id}', '').strip()
                penalty = int(penalty_raw) if penalty_raw.lstrip('-').isdigit() else 0

                if ext.id in current_ids:
                    member = current_by_id[ext.id]
                    if member.penalty != penalty:
                        member.penalty = penalty
                        member.save(update_fields=['penalty'])
                        changed = True
                else:
                    QueueMember.objects.create(
                        queue_name=queue_name, interface=f'PJSIP/{ext.id}',
                        membername=ext.assigned_user.full_name if ext.assigned_user else ext.id,
                        penalty=penalty,
                    )
                    changed = True

            if changed:
                # No dialplan rows to regenerate — membership is checked live (REALTIME_FIELD)
                # at call time, so a plain reload of Asterisk's in-memory queue state suffices.
                _queue_reload()
                messages.success(request, f'Updated members for "{queue_name}".')
        else:
            for field_errors in form.errors.values():
                for error in field_errors:
                    messages.error(request, error)
    return redirect('admin_settings_queue_edit', queue_name=queue_name)


@login_required
def admin_sms_view(request):
    return render(request, 'admin/sms.html', {'active': 'sms'})


def _whatsapp_api_post(path, payload):
    wa = WhatsAppSettings.load()
    req = urllib.request.Request(
        f'https://graph.facebook.com/v25.0/{path}',
        data=json.dumps(payload).encode(),
        headers={'Authorization': f'Bearer {wa.access_token}', 'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as e:
        return None, e.read().decode()
    except urllib.error.URLError as e:
        return None, str(e.reason)


def _whatsapp_send_message(number, body, sent_by=None):
    """Free-form text reply -- only actually delivers within Meta's 24h customer-service
    window (see WhatsAppConversation.last_customer_message_at); outside that window Meta
    rejects it and this is recorded as 'failed', same as any other send error. Broadcasting a
    pre-approved template to start/restart a conversation is a separate, not-yet-built feature
    (see the 'no.' answer -- two-way chat first)."""
    conversation, _ = WhatsAppConversation.objects.get_or_create(number=number)
    message = WhatsAppMessage.objects.create(conversation=conversation, direction='out', body=body, sent_by=sent_by)
    wa = WhatsAppSettings.load()
    result, error = _whatsapp_api_post(f'{wa.phone_number_id}/messages', {
        'messaging_product': 'whatsapp', 'to': number,
        'type': 'text', 'text': {'body': body},
    })
    if error or not result:
        message.status = 'failed'
    else:
        message.status = 'sent'
        message.wa_message_id = result.get('messages', [{}])[0].get('id', '')
    message.save()
    conversation.last_message_at = timezone.now()
    conversation.save(update_fields=['last_message_at'])
    _whatsapp_push_message(message)
    return message


def _whatsapp_media_type_for(content_type):
    for prefix in ('image', 'video', 'audio'):
        if content_type.startswith(f'{prefix}/'):
            return prefix
    return 'document'


def _whatsapp_upload_media(local_path, filename, content_type):
    """Meta requires a file to be uploaded to its Media API (getting back an opaque media_id)
    before it can be referenced in an outbound message -- unlike every other WhatsApp API call
    here this is multipart/form-data, not JSON, and urllib has no built-in support for building
    that, hence the manual boundary construction below instead of reusing _whatsapp_api_post."""
    wa = WhatsAppSettings.load()
    boundary = uuid.uuid4().hex
    with open(local_path, 'rb') as f:
        file_bytes = f.read()
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="messaging_product"\r\n\r\nwhatsapp\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f'Content-Type: {content_type}\r\n\r\n'
    ).encode() + file_bytes + f'\r\n--{boundary}--\r\n'.encode()
    req = urllib.request.Request(
        f'https://graph.facebook.com/v25.0/{wa.phone_number_id}/media',
        data=body,
        headers={
            'Authorization': f'Bearer {wa.access_token}',
            'Content-Type': f'multipart/form-data; boundary={boundary}',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode()).get('id'), None
    except urllib.error.HTTPError as e:
        return None, e.read().decode()
    except urllib.error.URLError as e:
        return None, str(e.reason)


def _whatsapp_download_media(media_id):
    """Inbound media arrives as just a media_id -- a two-step fetch: first resolve it to a
    short-lived signed URL, then download that URL with the same bearer token (the URL itself
    is not enough on its own). Returns (local_filename, mime_type) or (None, None) on any
    failure, since the webhook must never 500 back to Meta over a download hiccup."""
    wa = WhatsAppSettings.load()
    try:
        req = urllib.request.Request(
            f'https://graph.facebook.com/v25.0/{media_id}',
            headers={'Authorization': f'Bearer {wa.access_token}'},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            info = json.loads(resp.read().decode())
        media_url = info.get('url')
        mime_type = info.get('mime_type', 'application/octet-stream')
        if not media_url:
            return None, None
        req = urllib.request.Request(media_url, headers={'Authorization': f'Bearer {wa.access_token}'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            file_bytes = resp.read()
        ext = mimetypes.guess_extension(mime_type.split(';')[0].strip()) or ''
        local_name = f'{uuid.uuid4().hex}{ext}'
        with open(os.path.join(WHATSAPP_MEDIA_DIR, local_name), 'wb') as f:
            f.write(file_bytes)
        return local_name, mime_type
    except Exception:
        return None, None


def _transcode_to_opus_ogg(input_path):
    """Meta's WhatsApp Media API only accepts a fixed set of audio containers/codecs for a
    'voice message' bubble to render as one -- audio/ogg with the OPUS codec is the one every
    browser's raw MediaRecorder output (audio/webm;codecs=opus in Chrome, audio/ogg in Firefox,
    audio/mp4 in Safari) is NOT guaranteed to already be, so a mic recording is always run
    through this server-side re-encode rather than trusting the browser's container.

    ffmpeg's own Ogg/Opus muxer produces a file Meta's media processor rejects outright (error
    131053: 'uploaded with mimetype as audio/ogg; codecs=opus, however on processing it is of
    type application/octet-stream') -- confirmed live against the real API. opusenc's OggOpus
    output doesn't have this problem, but it only reads WAV/AIFF/FLAC, not arbitrary browser
    recording formats, hence the two-step pipeline: ffmpeg decodes whatever the browser sent
    into a plain mono WAV, then opusenc encodes that WAV into the strict OggOpus Meta expects.

    Returns the converted file's path, or None if either step fails (caller falls back to
    sending the original file as-is)."""
    wav_path = input_path + '.tmp.wav'
    output_path = input_path + '.converted.ogg'
    try:
        decode = subprocess.run(
            ['ffmpeg', '-y', '-i', input_path, '-vn', '-ac', '1', '-ar', '48000', wav_path],
            capture_output=True, timeout=30,
        )
        if decode.returncode != 0 or not os.path.isfile(wav_path):
            return None
        encode = subprocess.run(
            ['opusenc', '--bitrate', '32', '--downmix-mono', wav_path, output_path],
            capture_output=True, timeout=30,
        )
        if encode.returncode == 0 and os.path.isfile(output_path):
            return output_path
    except (subprocess.SubprocessError, OSError):
        pass
    finally:
        if os.path.isfile(wav_path):
            os.remove(wav_path)
    return None


def _whatsapp_send_media_message(number, uploaded_file, caption='', sent_by=None, is_voice=False):
    """Mirrors _whatsapp_send_message but for an outbound image/video/audio/document: save the
    agent's upload locally first (so it's on disk even if the Meta upload/send call below fails),
    then upload it to Meta and send a message referencing the resulting media_id.

    is_voice marks a mic-recorded voice note specifically (as opposed to an audio file picked via
    the attachment button) -- see _transcode_to_opus_ogg for why that path re-encodes first."""
    conversation, _ = WhatsAppConversation.objects.get_or_create(number=number)
    content_type = uploaded_file.content_type or 'application/octet-stream'
    media_type = _whatsapp_media_type_for(content_type)
    ext = os.path.splitext(uploaded_file.name)[1]
    local_name = f'{uuid.uuid4().hex}{ext}'
    local_path = os.path.join(WHATSAPP_MEDIA_DIR, local_name)
    with open(local_path, 'wb') as f:
        for chunk in uploaded_file.chunks():
            f.write(chunk)
    filename = uploaded_file.name

    if is_voice:
        converted_path = _transcode_to_opus_ogg(local_path)
        if converted_path:
            os.remove(local_path)
            local_name = f'{uuid.uuid4().hex}.ogg'
            local_path = os.path.join(WHATSAPP_MEDIA_DIR, local_name)
            os.rename(converted_path, local_path)
            content_type = 'audio/ogg'
            filename = 'voice-message.ogg'
            media_type = 'audio'

    message = WhatsAppMessage.objects.create(
        conversation=conversation, direction='out', body=caption, sent_by=sent_by,
        media_type=media_type, media_path=local_name, media_filename=filename,
        media_mime_type=content_type,
    )

    print(f'[whatsapp media] msg {message.pk}: saved {os.path.getsize(local_path)} bytes, is_voice={is_voice}, content_type={content_type}, media_type={media_type}')

    media_id, upload_error = _whatsapp_upload_media(local_path, filename, content_type)
    wa = WhatsAppSettings.load()
    if upload_error or not media_id:
        message.status = 'failed'
        print(f'[whatsapp media] msg {message.pk}: upload FAILED: {upload_error}')
    else:
        media_payload = {'id': media_id}
        if caption and media_type in ('image', 'video', 'document'):
            media_payload['caption'] = caption
        if media_type == 'document':
            media_payload['filename'] = filename
        result, error = _whatsapp_api_post(f'{wa.phone_number_id}/messages', {
            'messaging_product': 'whatsapp', 'to': number,
            'type': media_type, media_type: media_payload,
        })
        if error or not result:
            message.status = 'failed'
            print(f'[whatsapp media] msg {message.pk}: send FAILED (media_id={media_id}): {error}')
        else:
            message.status = 'sent'
            message.wa_message_id = result.get('messages', [{}])[0].get('id', '')
    message.save()
    conversation.last_message_at = timezone.now()
    conversation.save(update_fields=['last_message_at'])
    _whatsapp_push_message(message)
    return message


def _whatsapp_push(data):
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    channel_layer = get_channel_layer()
    if channel_layer is not None:
        async_to_sync(channel_layer.group_send)('realtime', {'type': 'realtime_update', 'data': {'kind': 'whatsapp_message', **data}})


def _whatsapp_message_payload(message):
    """Shared shape for a single message, used both for the websocket push (see
    _whatsapp_push_message) and the JSON response the sender's own fetch() gets back --
    admin/whatsapp.html's JS renders both the same way via appendBubble()."""
    from django.template.defaultfilters import date as date_filter
    return {
        'id': message.pk,
        'number': message.conversation_id,
        'contact_name': message.conversation.contact_name,
        'direction': message.direction,
        'body': message.body,
        'status': message.status,
        'time': date_filter(timezone.localtime(message.created_at), 'g:i A'),
        'media_type': message.media_type,
        'media_filename': message.media_filename,
        'media_url': reverse('user_whatsapp_media', args=[message.pk]) if message.media_path else '',
    }


def _whatsapp_push_message(message):
    """Pushes enough of a single message over the websocket for the browser to append it (and
    update the conversation-list preview) in place -- see admin/whatsapp.html's socket handler --
    instead of every open WhatsApp tab doing a full page reload for each message."""
    _whatsapp_push(_whatsapp_message_payload(message))


def _assign_whatsapp_conversation():
    """Picks who a brand-new WhatsApp conversation goes to, per Settings -> WhatsApp's
    configured mode (WhatsAppSettings.assignment_mode). Returns None for 'shared' (stays
    unassigned -- any agent can pick it up, see _whatsapp_handle_agent_action's claim-on-reply)
    and 'manual' (stays unassigned until an admin assigns it from the WhatsApp report page), and
    also None if nobody happens to be logged in right now -- round_robin/least_busy always only
    consider agents currently logged in (see Activity), so a new message never lands on someone
    who isn't actually at their desk to answer it.

    No separate 'sticky' mode -- number is WhatsAppConversation's own primary key, so a
    returning customer's messages always land back on the same already-assigned row regardless
    of mode; this function only ever runs once per number, the first time it's ever seen."""
    wa = WhatsAppSettings.load()
    if wa.assignment_mode in ('shared', 'manual'):
        return None

    online_ids = set(Activity.objects.filter(logout_at__isnull=True).values_list('user_id', flat=True))
    agents = list(User.objects.filter(role='user', id__in=online_ids).order_by('id'))
    if not agents:
        return None

    if wa.assignment_mode == 'least_busy':
        counts = {a.id: 0 for a in agents}
        for row in WhatsAppConversation.objects.filter(assigned_user__in=agents).values('assigned_user').annotate(n=Count('number')):
            counts[row['assigned_user']] = row['n']
        return min(agents, key=lambda a: counts[a.id])

    # round_robin (default) -- based on how many conversations have been assigned to this same
    # eligible pool so far, rather than a separate 'last assigned' pointer, so it stays correct
    # with zero extra state to keep in sync.
    already_assigned = WhatsAppConversation.objects.filter(assigned_user__in=agents).count()
    return agents[already_assigned % len(agents)]


def _media_aware_preview(message):
    """A conversation-list preview line for the last message -- plain text messages just show
    their body, but a media message (image/video/audio/document/file) has an empty body, so
    without this the WhatsApp/Facebook sidebars and the Dashboard's Messages cards would show a
    blank preview instead of e.g. '🎵 Audio'. Shared by both WhatsAppMessage and FacebookMessage
    since they use the same field names (media_type differs only in 'document' vs 'file')."""
    if not message:
        return ''
    if message.body:
        return message.body
    if message.media_type == 'image':
        return '📷 Photo'
    if message.media_type == 'video':
        return '🎥 Video'
    if message.media_type == 'audio':
        return '🎵 Audio'
    if message.media_type in ('document', 'file'):
        return '📄 ' + (message.media_filename or 'Document')
    return ''


def _whatsapp_conversations_context(conversations_qs, selected_number):
    """Conversation list (with unread counts) + selected thread, shared by the admin-wide inbox
    and each agent's own assigned-conversations inbox -- same logic, different starting
    queryset. Marks the selected conversation read as a side effect, same as opening a chat app."""
    conversations = list(conversations_qs)
    selected_number = selected_number or (conversations[0].number if conversations else None)
    selected = next((c for c in conversations if c.number == selected_number), None)
    thread = selected.messages.select_related('sent_by').all() if selected else []

    for c in conversations:
        if c is selected:
            c.unread_count = 0
        elif c.last_read_at:
            c.unread_count = c.messages.filter(direction='in', created_at__gt=c.last_read_at).count()
        else:
            c.unread_count = c.messages.filter(direction='in').count()
        c.last_message_preview = _media_aware_preview(c.messages.last())

    if selected:
        selected.last_read_at = timezone.now()
        selected.save(update_fields=['last_read_at'])

    return conversations, selected, thread


def _whatsapp_handle_agent_action(request):
    """Send/mark_read/typing for one of the agent's own WhatsApp conversations -- used by
    user_dashboard_view's WhatsApp tab. Same three actions admin_whatsapp_view used to handle
    before it became report-only.

    In 'shared' assignment mode (see WhatsAppSettings.assignment_mode) an unassigned
    conversation is fair game for any agent -- replying to one claims it for that agent from
    then on, same as a real shared inbox, so a second agent can't also reply into it afterward."""
    number = request.POST.get('number', '').strip()
    conversation = WhatsAppConversation.objects.filter(number=number).first()
    if not conversation:
        return JsonResponse({'error': 'not found'}, status=404)
    is_owner = conversation.assigned_user_id == request.user.id
    is_claimable = conversation.assigned_user_id is None and WhatsAppSettings.load().assignment_mode == 'shared'
    if not (is_owner or is_claimable):
        return JsonResponse({'error': 'not assigned to you'}, status=403)
    action = request.POST.get('action')
    if action == 'send':
        body = request.POST.get('body', '').strip()
        if not body:
            return JsonResponse({'error': 'missing body'}, status=400)
        if is_claimable:
            conversation.assigned_user = request.user
            conversation.save(update_fields=['assigned_user'])
        message = _whatsapp_send_message(number, body, sent_by=request.user)
        return JsonResponse(_whatsapp_message_payload(message))
    if action == 'send_media':
        uploaded_file = request.FILES.get('file')
        if not uploaded_file:
            return JsonResponse({'error': 'missing file'}, status=400)
        if is_claimable:
            conversation.assigned_user = request.user
            conversation.save(update_fields=['assigned_user'])
        message = _whatsapp_send_media_message(
            number, uploaded_file, caption=request.POST.get('body', '').strip(), sent_by=request.user,
            is_voice=request.POST.get('is_voice') == '1',
        )
        return JsonResponse(_whatsapp_message_payload(message))
    if action == 'mark_read':
        WhatsAppConversation.objects.filter(number=number).update(last_read_at=timezone.now())
        return HttpResponse(status=204)
    if action == 'typing':
        # Meta has no "customer is typing" signal for us to receive, but it does support the
        # reverse: marking the customer's last message read with a typing_indicator shows *them*
        # an "typing..." hint for ~25s. Requires that message's wamid, so this is a no-op until
        # the customer has sent at least one message in this conversation.
        last_inbound = (
            WhatsAppMessage.objects.filter(conversation_id=number, direction='in')
            .exclude(wa_message_id='').order_by('-created_at').first()
        )
        if last_inbound:
            wa = WhatsAppSettings.load()
            _whatsapp_api_post(f'{wa.phone_number_id}/messages', {
                'messaging_product': 'whatsapp',
                'status': 'read',
                'message_id': last_inbound.wa_message_id,
                'typing_indicator': {'type': 'text'},
            })
        return HttpResponse(status=204)
    return HttpResponse(status=400)


@csrf_exempt
def whatsapp_webhook_view(request):
    """Meta calls this directly (no login, no CSRF token) -- see Settings -> WhatsApp for the
    verify token entered here matching what's configured in the Meta app's webhook setup, and
    app_secret for the signature check below."""
    wa = WhatsAppSettings.load()
    if request.method == 'GET':
        if (request.GET.get('hub.mode') == 'subscribe'
                and request.GET.get('hub.verify_token') == wa.verify_token
                and wa.verify_token):
            return HttpResponse(request.GET.get('hub.challenge', ''))
        return HttpResponseForbidden()

    if wa.app_secret:
        signature = request.META.get('HTTP_X_HUB_SIGNATURE_256', '')
        expected = 'sha256=' + hmac.new(wa.app_secret.encode(), request.body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return HttpResponseForbidden()

    try:
        payload = json.loads(request.body.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponse(status=200)

    # Meta expects a 200 no matter what -- repeated non-2xx responses get a webhook flagged
    # unreliable and eventually unsubscribed, so a malformed/unexpected payload shape must
    # never surface as a 500 here (this is the one place in the app that trusts a completely
    # external, unauthenticated-until-just-verified payload, so defensive parsing throughout
    # this block is deliberate, unlike the rest of the codebase's normal internal-trust rule).
    try:
        for entry in payload.get('entry', []):
            for change in entry.get('changes', []):
                value = change.get('value', {})
                contacts = {c['wa_id']: c.get('profile', {}).get('name', '') for c in value.get('contacts', []) if c.get('wa_id')}

                for msg in value.get('messages', []):
                    number = msg.get('from')
                    if not number:
                        continue
                    conversation, created = WhatsAppConversation.objects.get_or_create(number=number)
                    if created:
                        conversation.assigned_user = _assign_whatsapp_conversation()
                    if contacts.get(number):
                        conversation.contact_name = contacts[number]
                    now = timezone.now()
                    conversation.last_message_at = now
                    conversation.last_customer_message_at = now
                    conversation.save()

                    msg_type = msg.get('type', 'text')
                    media_type, media_path, media_filename, media_mime_type = '', '', '', ''
                    if msg_type == 'text':
                        body = msg.get('text', {}).get('body', '')
                    elif msg_type in ('image', 'video', 'audio', 'document'):
                        media_meta = msg.get(msg_type, {})
                        body = media_meta.get('caption', '')
                        local_name, mime_type = _whatsapp_download_media(media_meta.get('id', ''))
                        if local_name:
                            media_type = msg_type
                            media_path = local_name
                            media_filename = media_meta.get('filename', '') or local_name
                            media_mime_type = mime_type or ''
                        else:
                            body = body or f'[{msg_type}]'
                    else:
                        body = f'[{msg_type}]'

                    new_message = WhatsAppMessage.objects.create(
                        conversation=conversation, direction='in', body=body,
                        wa_message_id=msg.get('id', ''), status='received',
                        media_type=media_type, media_path=media_path,
                        media_filename=media_filename, media_mime_type=media_mime_type,
                    )
                    _whatsapp_push_message(new_message)

                for status in value.get('statuses', []):
                    if status.get('status') == 'failed':
                        print(f"[whatsapp status] {status.get('id', '')} FAILED: {status.get('errors', [])}")
                    WhatsAppMessage.objects.filter(wa_message_id=status.get('id', '')).update(status=status.get('status', 'sent'))
    except Exception:
        pass

    return HttpResponse(status=200)


def _whatsapp_report_stats(day):
    """One day's WhatsApp numbers for the admin report -- actual conversations are handled by
    whichever agent they're round-robin assigned to (see _assign_whatsapp_conversation); admin
    only gets this aggregate view for the selected day, not the live inbox itself."""
    messages = WhatsAppMessage.objects.filter(created_at__date=day)
    # Only agents worth showing right now: currently logged in to the dashboard (see Activity --
    # login_view/logout_view, not PJSIP registration), or ones who actually sent a WhatsApp
    # message on the selected day -- not every agent account that's ever existed.
    currently_logged_in = set(Activity.objects.filter(logout_at__isnull=True).values_list('user_id', flat=True))

    per_agent = []
    for agent in User.objects.filter(role='user').order_by('full_name'):
        agent_sent = messages.filter(direction='out', sent_by=agent)
        sent_count = agent_sent.count()
        if agent.id not in currently_logged_in and sent_count == 0:
            continue
        last_sent = agent_sent.order_by('-created_at').first()
        per_agent.append({
            'name': agent.full_name or agent.username,
            'active_conversations': WhatsAppConversation.objects.filter(
                assigned_user=agent, messages__created_at__date=day
            ).distinct().count(),
            'messages_sent': sent_count,
            'last_activity': timezone.localtime(last_sent.created_at) if last_sent else None,
        })
    per_agent.sort(key=lambda r: r['messages_sent'], reverse=True)

    return {
        'total_conversations': messages.values('conversation_id').distinct().count(),
        'total_received': messages.filter(direction='in').count(),
        'total_sent': messages.filter(direction='out').count(),
        'per_agent': per_agent,
    }


@login_required
def admin_whatsapp_view(request):
    if request.method == 'POST' and request.POST.get('action') == 'assign':
        number = request.POST.get('number', '').strip()
        agent = User.objects.filter(id=request.POST.get('agent_id'), role='user').first()
        if agent:
            WhatsAppConversation.objects.filter(number=number).update(assigned_user=agent)
            messages.success(request, 'Conversation assigned.')
        else:
            messages.error(request, 'Choose an agent to assign.')
        return redirect('admin_whatsapp')

    today_local = _local_today()
    date_str = request.GET.get('date') or today_local.isoformat()
    try:
        day = datetime.date.fromisoformat(date_str)
    except ValueError:
        day = today_local
        date_str = day.isoformat()
    return render(request, 'admin/whatsapp.html', {
        'active': 'whatsapp', 'date': date_str,
        # Shown regardless of the configured mode -- 'manual' always leaves these for the admin
        # to hand out below, but 'round_robin'/'least_busy' can strand one here too if nobody
        # was logged in when it came in.
        'unassigned_conversations': WhatsAppConversation.objects.filter(assigned_user__isnull=True),
        'agents': User.objects.filter(role='user').order_by('full_name'),
        **_whatsapp_report_stats(day),
    })


def _normalize_phone(raw):
    """Digits only -- no assumed country/region. Matching (see agi.router._is_blacklisted) then
    compares two numbers by whichever is shorter being a suffix of the other, so a blocked
    number matches regardless of how it or the caller ID happened to include (or not include) a
    country code, and works the same for every country's numbering plan without configuration."""
    digits = re.sub(r'\D', '', raw or '')
    return digits or None


def _extract_lead_numbers(request):
    """Pulls phone numbers out of the "numbers" textarea and/or an uploaded file (see
    admin/leads.html and admin/lead_list_detail.html's combined upload field). .xlsx is read
    cell-by-cell via openpyxl since it's a zip container, not text; .csv/.txt (or the textarea)
    are just split on commas/newlines -- either way every cell/line is normalized and only
    valid numbers survive, so a header row or a name column alongside the phone column is
    harmless."""
    upload = request.FILES.get('file')
    raw_text = request.POST.get('numbers', '')

    if upload and upload.name.lower().endswith('.xlsx'):
        from openpyxl import load_workbook
        workbook = load_workbook(upload, read_only=True, data_only=True)
        tokens = [
            str(cell) for sheet in workbook.worksheets
            for row in sheet.iter_rows(values_only=True)
            for cell in row if cell is not None
        ]
    else:
        if upload:
            raw_text = upload.read().decode('utf-8', errors='ignore')
        tokens = re.split(r'[,\r\n]+', raw_text)

    numbers = {_normalize_phone(t) for t in tokens}
    numbers.discard(None)
    return numbers


@login_required
def admin_settings_blacklist_view(request):
    # DND/blacklist entries affect every caller company-wide (see _is_blacklisted in
    # agi/router.py) -- unlike most Settings pages, this one is admin-only in practice, not just
    # by convention, since a regular agent could otherwise silently block callers for everyone.
    if request.user.role != 'admin':
        messages.error(request, 'You do not have permission to access this page.')
        return redirect('user_dashboard')

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'add':
            number = _normalize_phone(request.POST.get('number', ''))
            reason = request.POST.get('reason', '').strip()
            if not number:
                messages.error(request, 'Enter a valid phone number.')
            else:
                Blacklist.objects.update_or_create(
                    number=number, defaults={'reason': reason, 'added_by': request.user},
                )
                messages.success(request, f'{number} added to the blacklist.')
            return redirect('admin_settings_blacklist')

        if action == 'delete':
            Blacklist.objects.filter(id=request.POST.get('id')).delete()
            messages.success(request, 'Number removed from the blacklist.')
            return redirect('admin_settings_blacklist')

    entries = Blacklist.objects.select_related('added_by').order_by('-created_at')
    return render(request, 'admin/settings_blacklist.html', {
        'active': 'settings', 'settings_active': 'blacklist', 'entries': entries,
    })


@login_required
def admin_settings_whatsapp_view(request):
    if request.method == 'POST' and request.POST.get('action') == 'save_whatsapp_settings':
        wa = WhatsAppSettings.load()
        wa.phone_number_id = request.POST.get('phone_number_id', '').strip()
        wa.waba_id = request.POST.get('waba_id', '').strip()
        wa.access_token = request.POST.get('access_token', '').strip()
        wa.app_secret = request.POST.get('app_secret', '').strip()
        wa.verify_token = request.POST.get('verify_token', '').strip()
        assignment_mode = request.POST.get('assignment_mode', '').strip()
        if assignment_mode in dict(WhatsAppSettings.ASSIGNMENT_MODE_CHOICES):
            wa.assignment_mode = assignment_mode
        wa.save()
        messages.success(request, 'WhatsApp settings saved.')
        return redirect('admin_settings_whatsapp')

    if request.method == 'POST' and request.POST.get('action') == 'resubscribe_waba':
        wa = WhatsAppSettings.load()
        if not wa.waba_id or not wa.access_token:
            messages.error(request, 'WhatsApp Business Account ID and Access Token are both required first.')
            return redirect('admin_settings_whatsapp')
        req = urllib.request.Request(
            f'https://graph.facebook.com/v25.0/{wa.waba_id}/subscribed_apps',
            data=b'', headers={'Authorization': f'Bearer {wa.access_token}'}, method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode())
            if result.get('success'):
                messages.success(request, 'This app is now subscribed to receive webhook events for that WhatsApp Business Account.')
            else:
                messages.error(request, f'Meta did not confirm the subscription: {result}')
        except urllib.error.HTTPError as e:
            messages.error(request, f'Meta rejected the request: {e.read().decode()}')
        except urllib.error.URLError as e:
            messages.error(request, f'Could not reach Meta: {e.reason}')
        return redirect('admin_settings_whatsapp')

    return render(request, 'admin/settings_whatsapp.html', {
        'active': 'settings', 'settings_active': 'whatsapp',
        'wa_settings': WhatsAppSettings.load(),
        'webhook_url': request.build_absolute_uri(reverse('whatsapp_webhook')),
    })


@login_required
def user_whatsapp_media_view(request, message_id):
    """Serves one WhatsApp message's locally-stored media file -- mirrors
    admin_cdr_recording_view's pattern (raw filesystem storage outside Django's media framework),
    gated to the conversation's assigned agent so one agent can't pull another's media by guessing
    an id."""
    message = get_object_or_404(WhatsAppMessage, pk=message_id)
    conversation = message.conversation
    is_owner = conversation.assigned_user_id == request.user.id
    is_shared_unclaimed = conversation.assigned_user_id is None and WhatsAppSettings.load().assignment_mode == 'shared'
    if not (is_owner or is_shared_unclaimed):
        return HttpResponseForbidden()
    if not message.media_path:
        raise Http404
    path = os.path.join(WHATSAPP_MEDIA_DIR, message.media_path)
    if not os.path.isfile(path):
        raise Http404
    return FileResponse(open(path, 'rb'), content_type=message.media_mime_type or 'application/octet-stream')


def _facebook_api_post(path, payload):
    fb = FacebookSettings.load()
    req = urllib.request.Request(
        f'https://graph.facebook.com/v19.0/{path}',
        data=json.dumps(payload).encode(),
        headers={'Authorization': f'Bearer {fb.page_access_token}', 'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as e:
        return None, e.read().decode()
    except urllib.error.URLError as e:
        return None, str(e.reason)


def _facebook_send_message(psid, body, sent_by=None):
    """Free-form text reply -- only actually delivers within Meta's 24h messaging window (see
    FacebookConversation.last_customer_message_at), same rule and same failure handling as
    _whatsapp_send_message."""
    conversation, _ = FacebookConversation.objects.get_or_create(psid=psid)
    message = FacebookMessage.objects.create(conversation=conversation, direction='out', body=body, sent_by=sent_by)
    result, error = _facebook_api_post('me/messages', {
        'recipient': {'id': psid},
        'message': {'text': body},
        'messaging_type': 'RESPONSE',
    })
    if error or not result:
        message.status = 'failed'
    else:
        message.status = 'sent'
        message.fb_message_id = result.get('message_id', '')
    message.save()
    conversation.last_message_at = timezone.now()
    conversation.save(update_fields=['last_message_at'])
    _facebook_push_message(message)
    return message


def _facebook_media_type_for(content_type):
    """Messenger's Send API attachment 'type' values -- 'file' (not 'document') is its name for
    anything else, unlike WhatsApp's Media API."""
    for prefix in ('image', 'video', 'audio'):
        if content_type.startswith(f'{prefix}/'):
            return prefix
    return 'file'


def _facebook_send_media_attachment(psid, local_path, filename, content_type, media_type):
    """Unlike WhatsApp's two-step upload-then-reference-by-id flow, Messenger's Send API uploads
    and sends an attachment in one multipart call straight to /me/messages -- no separate media
    endpoint or media_id needed."""
    fb = FacebookSettings.load()
    boundary = uuid.uuid4().hex
    with open(local_path, 'rb') as f:
        file_bytes = f.read()
    message_json = json.dumps({'attachment': {'type': media_type, 'payload': {}}})
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="recipient"\r\n\r\n{json.dumps({"id": psid})}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="messaging_type"\r\n\r\nRESPONSE\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="message"\r\n\r\n{message_json}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="filedata"; filename="{filename}"\r\n'
        f'Content-Type: {content_type}\r\n\r\n'
    ).encode() + file_bytes + f'\r\n--{boundary}--\r\n'.encode()
    req = urllib.request.Request(
        'https://graph.facebook.com/v19.0/me/messages',
        data=body,
        headers={
            'Authorization': f'Bearer {fb.page_access_token}',
            'Content-Type': f'multipart/form-data; boundary={boundary}',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as e:
        return None, e.read().decode()
    except urllib.error.URLError as e:
        return None, str(e.reason)


def _facebook_download_media(url):
    """Unlike WhatsApp's media_id -> signed-URL -> download dance, Messenger's inbound webhook
    hands over a directly-fetchable CDN url for each attachment right in the payload -- no
    bearer token needed, just GET it. Returns (local_filename, mime_type) or (None, None) on any
    failure, since the webhook must never 500 back to Meta over a download hiccup."""
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            mime_type = resp.headers.get('Content-Type', 'application/octet-stream').split(';')[0].strip()
            file_bytes = resp.read()
        ext = mimetypes.guess_extension(mime_type) or ''
        local_name = f'{uuid.uuid4().hex}{ext}'
        with open(os.path.join(FACEBOOK_MEDIA_DIR, local_name), 'wb') as f:
            f.write(file_bytes)
        return local_name, mime_type
    except Exception:
        return None, None


def _facebook_send_media_message(psid, uploaded_file, caption='', sent_by=None, is_voice=False):
    """Mirrors _whatsapp_send_media_message, adapted for Messenger: save the agent's upload
    locally, transcode it if it's a mic recording (see _transcode_to_opus_ogg -- Messenger isn't
    as strict about audio format as WhatsApp, but the same re-encode keeps it consistently
    playable everywhere), then hand it to Meta. Unlike WhatsApp, Messenger has no inline
    caption-with-attachment message shape, so a caption is sent as its own separate follow-up
    text message instead of being attached to this one."""
    conversation, _ = FacebookConversation.objects.get_or_create(psid=psid)
    content_type = uploaded_file.content_type or 'application/octet-stream'
    media_type = _facebook_media_type_for(content_type)
    ext = os.path.splitext(uploaded_file.name)[1]
    local_name = f'{uuid.uuid4().hex}{ext}'
    local_path = os.path.join(FACEBOOK_MEDIA_DIR, local_name)
    with open(local_path, 'wb') as f:
        for chunk in uploaded_file.chunks():
            f.write(chunk)
    filename = uploaded_file.name

    if is_voice:
        converted_path = _transcode_to_opus_ogg(local_path)
        if converted_path:
            os.remove(local_path)
            local_name = f'{uuid.uuid4().hex}.ogg'
            local_path = os.path.join(FACEBOOK_MEDIA_DIR, local_name)
            os.rename(converted_path, local_path)
            content_type = 'audio/ogg'
            filename = 'voice-message.ogg'
            media_type = 'audio'

    message = FacebookMessage.objects.create(
        conversation=conversation, direction='out', body='', sent_by=sent_by,
        media_type=media_type, media_path=local_name, media_filename=filename,
        media_mime_type=content_type,
    )

    result, error = _facebook_send_media_attachment(psid, local_path, filename, content_type, media_type)
    if error or not result:
        message.status = 'failed'
    else:
        message.status = 'sent'
        message.fb_message_id = result.get('message_id', '')
    message.save()
    conversation.last_message_at = timezone.now()
    conversation.save(update_fields=['last_message_at'])
    _facebook_push_message(message)

    if caption.strip():
        _facebook_send_message(psid, caption.strip(), sent_by=sent_by)

    return message


def _facebook_push(data):
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    channel_layer = get_channel_layer()
    if channel_layer is not None:
        async_to_sync(channel_layer.group_send)('realtime', {'type': 'realtime_update', 'data': {'kind': 'facebook_message', **data}})


def _facebook_message_payload(message):
    """Shared shape for a single message, used both for the websocket push (see
    _facebook_push_message) and the JSON response the sender's own fetch() gets back --
    user/facebook.html's JS renders both the same way via appendBubble()."""
    from django.template.defaultfilters import date as date_filter
    return {
        'id': message.pk,
        'psid': message.conversation_id,
        'contact_name': message.conversation.contact_name,
        'direction': message.direction,
        'body': message.body,
        'status': message.status,
        'time': date_filter(timezone.localtime(message.created_at), 'g:i A'),
        'media_type': message.media_type,
        'media_filename': message.media_filename,
        'media_url': reverse('user_facebook_media', args=[message.pk]) if message.media_path else '',
    }


def _facebook_push_message(message):
    _facebook_push(_facebook_message_payload(message))


def _assign_facebook_conversation():
    """Picks who a brand-new Facebook Messenger conversation goes to -- a straight copy of
    _assign_whatsapp_conversation with the model names swapped (see that function's docstring
    for the full reasoning, including why there's no separate 'sticky' mode)."""
    fb = FacebookSettings.load()
    if fb.assignment_mode in ('shared', 'manual'):
        return None

    online_ids = set(Activity.objects.filter(logout_at__isnull=True).values_list('user_id', flat=True))
    agents = list(User.objects.filter(role='user', id__in=online_ids).order_by('id'))
    if not agents:
        return None

    if fb.assignment_mode == 'least_busy':
        counts = {a.id: 0 for a in agents}
        for row in FacebookConversation.objects.filter(assigned_user__in=agents).values('assigned_user').annotate(n=Count('psid')):
            counts[row['assigned_user']] = row['n']
        return min(agents, key=lambda a: counts[a.id])

    already_assigned = FacebookConversation.objects.filter(assigned_user__in=agents).count()
    return agents[already_assigned % len(agents)]


def _facebook_conversations_context(conversations_qs, selected_psid):
    """Conversation list (with unread counts) + selected thread -- a straight copy of
    _whatsapp_conversations_context with the model/field names swapped."""
    conversations = list(conversations_qs)
    selected_psid = selected_psid or (conversations[0].psid if conversations else None)
    selected = next((c for c in conversations if c.psid == selected_psid), None)
    thread = selected.messages.select_related('sent_by').all() if selected else []

    for c in conversations:
        if c is selected:
            c.unread_count = 0
        elif c.last_read_at:
            c.unread_count = c.messages.filter(direction='in', created_at__gt=c.last_read_at).count()
        else:
            c.unread_count = c.messages.filter(direction='in').count()
        c.last_message_preview = _media_aware_preview(c.messages.last())

    if selected:
        selected.last_read_at = timezone.now()
        selected.save(update_fields=['last_read_at'])

    return conversations, selected, thread


def _facebook_handle_agent_action(request):
    """Send/mark_read for one of the agent's own Facebook conversations -- used by
    user_dashboard_view's Facebook tab. Same claim-on-reply rule as
    _whatsapp_handle_agent_action for 'shared' assignment mode. No 'typing' action -- Messenger's
    typing-indicator endpoint needs no prior inbound message id the way WhatsApp's does, but
    wiring it up wasn't asked for, so it's left out rather than half-built."""
    psid = request.POST.get('psid', '').strip()
    conversation = FacebookConversation.objects.filter(psid=psid).first()
    if not conversation:
        return JsonResponse({'error': 'not found'}, status=404)
    is_owner = conversation.assigned_user_id == request.user.id
    is_claimable = conversation.assigned_user_id is None and FacebookSettings.load().assignment_mode == 'shared'
    if not (is_owner or is_claimable):
        return JsonResponse({'error': 'not assigned to you'}, status=403)
    action = request.POST.get('action')
    if action == 'send':
        body = request.POST.get('body', '').strip()
        if not body:
            return JsonResponse({'error': 'missing body'}, status=400)
        if is_claimable:
            conversation.assigned_user = request.user
            conversation.save(update_fields=['assigned_user'])
        message = _facebook_send_message(psid, body, sent_by=request.user)
        return JsonResponse(_facebook_message_payload(message))
    if action == 'send_media':
        uploaded_file = request.FILES.get('file')
        if not uploaded_file:
            return JsonResponse({'error': 'missing file'}, status=400)
        if is_claimable:
            conversation.assigned_user = request.user
            conversation.save(update_fields=['assigned_user'])
        message = _facebook_send_media_message(
            psid, uploaded_file, caption=request.POST.get('body', '').strip(), sent_by=request.user,
            is_voice=request.POST.get('is_voice') == '1',
        )
        return JsonResponse(_facebook_message_payload(message))
    if action == 'mark_read':
        FacebookConversation.objects.filter(psid=psid).update(last_read_at=timezone.now())
        return HttpResponse(status=204)
    return HttpResponse(status=400)


@csrf_exempt
def facebook_webhook_view(request):
    """Meta calls this directly (no login, no CSRF token) -- see Settings -> Facebook for the
    verify token entered here matching what's configured in the Meta app's webhook setup, and
    app_secret for the signature check below. Same verify/signature dance as
    whatsapp_webhook_view (both are Meta Graph API webhooks), just a different payload shape:
    entry[].messaging[] instead of entry[].changes[].value."""
    fb = FacebookSettings.load()
    if request.method == 'GET':
        if (request.GET.get('hub.mode') == 'subscribe'
                and request.GET.get('hub.verify_token') == fb.verify_token
                and fb.verify_token):
            return HttpResponse(request.GET.get('hub.challenge', ''))
        return HttpResponseForbidden()

    if fb.app_secret:
        signature = request.META.get('HTTP_X_HUB_SIGNATURE_256', '')
        expected = 'sha256=' + hmac.new(fb.app_secret.encode(), request.body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return HttpResponseForbidden()

    try:
        payload = json.loads(request.body.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponse(status=200)

    # Meta expects a 200 no matter what -- same reasoning as whatsapp_webhook_view's identical
    # comment: a malformed/unexpected payload shape must never surface as a 500 here.
    try:
        for entry in payload.get('entry', []):
            for event in entry.get('messaging', []):
                psid = event.get('sender', {}).get('id')
                message_data = event.get('message')
                if not psid or not message_data or message_data.get('is_echo'):
                    continue
                conversation, created = FacebookConversation.objects.get_or_create(psid=psid)
                if created:
                    conversation.assigned_user = _assign_facebook_conversation()
                    # Best-effort only -- needs the pages_messaging permission's profile access
                    # and a reachable Graph API, neither guaranteed at webhook time, so a failure
                    # here just leaves contact_name blank (falls back to showing the raw PSID)
                    # rather than ever failing the webhook itself.
                    try:
                        req = urllib.request.Request(
                            f'https://graph.facebook.com/v19.0/{psid}?fields=first_name,last_name&access_token={fb.page_access_token}'
                        )
                        with urllib.request.urlopen(req, timeout=5) as resp:
                            profile = json.loads(resp.read().decode())
                        name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
                        if name:
                            conversation.contact_name = name
                    except Exception:
                        pass
                now = timezone.now()
                conversation.last_message_at = now
                conversation.last_customer_message_at = now
                conversation.save()

                body = message_data.get('text', '')
                media_type, media_path, media_filename, media_mime_type = '', '', '', ''
                attachments = message_data.get('attachments', [])
                if attachments:
                    attachment = attachments[0]
                    fb_attach_type = attachment.get('type', 'file')
                    attach_url = attachment.get('payload', {}).get('url', '')
                    local_name, mime_type = _facebook_download_media(attach_url) if attach_url else (None, None)
                    if local_name:
                        media_type = fb_attach_type if fb_attach_type in ('image', 'video', 'audio') else 'file'
                        media_path = local_name
                        media_filename = local_name
                        media_mime_type = mime_type or ''
                    else:
                        body = body or f'[{fb_attach_type}]'
                elif not body:
                    body = '[message]'

                new_message = FacebookMessage.objects.create(
                    conversation=conversation, direction='in', body=body,
                    fb_message_id=message_data.get('mid', ''), status='received',
                    media_type=media_type, media_path=media_path,
                    media_filename=media_filename, media_mime_type=media_mime_type,
                )
                _facebook_push_message(new_message)

            for event in entry.get('messaging', []):
                delivery = event.get('delivery')
                if delivery:
                    FacebookMessage.objects.filter(fb_message_id__in=delivery.get('mids', [])).update(status='delivered')
                read = event.get('read')
                if read:
                    psid = event.get('sender', {}).get('id')
                    FacebookMessage.objects.filter(conversation_id=psid, direction='out', status__in=('sent', 'delivered')).update(status='read')
    except Exception:
        pass

    return HttpResponse(status=200)


def _facebook_report_stats(day):
    """One day's Facebook Messenger numbers for the admin report -- a straight copy of
    _whatsapp_report_stats with the model/field names swapped."""
    messages = FacebookMessage.objects.filter(created_at__date=day)
    currently_logged_in = set(Activity.objects.filter(logout_at__isnull=True).values_list('user_id', flat=True))

    per_agent = []
    for agent in User.objects.filter(role='user').order_by('full_name'):
        agent_sent = messages.filter(direction='out', sent_by=agent)
        sent_count = agent_sent.count()
        if agent.id not in currently_logged_in and sent_count == 0:
            continue
        last_sent = agent_sent.order_by('-created_at').first()
        per_agent.append({
            'name': agent.full_name or agent.username,
            'active_conversations': FacebookConversation.objects.filter(
                assigned_user=agent, messages__created_at__date=day
            ).distinct().count(),
            'messages_sent': sent_count,
            'last_activity': timezone.localtime(last_sent.created_at) if last_sent else None,
        })
    per_agent.sort(key=lambda r: r['messages_sent'], reverse=True)

    return {
        'total_conversations': messages.values('conversation_id').distinct().count(),
        'total_received': messages.filter(direction='in').count(),
        'total_sent': messages.filter(direction='out').count(),
        'per_agent': per_agent,
    }


@login_required
def admin_facebook_view(request):
    if request.method == 'POST' and request.POST.get('action') == 'assign':
        psid = request.POST.get('psid', '').strip()
        agent = User.objects.filter(id=request.POST.get('agent_id'), role='user').first()
        if agent:
            FacebookConversation.objects.filter(psid=psid).update(assigned_user=agent)
            messages.success(request, 'Conversation assigned.')
        else:
            messages.error(request, 'Choose an agent to assign.')
        return redirect('admin_facebook')

    today_local = _local_today()
    date_str = request.GET.get('date') or today_local.isoformat()
    try:
        day = datetime.date.fromisoformat(date_str)
    except ValueError:
        day = today_local
        date_str = day.isoformat()
    return render(request, 'admin/facebook.html', {
        'active': 'facebook', 'date': date_str,
        'unassigned_conversations': FacebookConversation.objects.filter(assigned_user__isnull=True),
        'agents': User.objects.filter(role='user').order_by('full_name'),
        **_facebook_report_stats(day),
    })


@login_required
def admin_settings_facebook_view(request):
    if request.method == 'POST' and request.POST.get('action') == 'save_facebook_settings':
        fb = FacebookSettings.load()
        fb.page_id = request.POST.get('page_id', '').strip()
        fb.page_access_token = request.POST.get('page_access_token', '').strip()
        fb.app_secret = request.POST.get('app_secret', '').strip()
        fb.verify_token = request.POST.get('verify_token', '').strip()
        assignment_mode = request.POST.get('assignment_mode', '').strip()
        if assignment_mode in dict(FacebookSettings.ASSIGNMENT_MODE_CHOICES):
            fb.assignment_mode = assignment_mode
        fb.save()
        messages.success(request, 'Facebook settings saved.')
        return redirect('admin_settings_facebook')
    return render(request, 'admin/settings_facebook.html', {
        'active': 'settings', 'settings_active': 'facebook',
        'fb_settings': FacebookSettings.load(),
        'webhook_url': request.build_absolute_uri(reverse('facebook_webhook')),
    })


@login_required
def user_facebook_media_view(request, message_id):
    """Serves one Facebook message's locally-stored media file -- a straight copy of
    user_whatsapp_media_view with the model names swapped."""
    message = get_object_or_404(FacebookMessage, pk=message_id)
    conversation = message.conversation
    is_owner = conversation.assigned_user_id == request.user.id
    is_shared_unclaimed = conversation.assigned_user_id is None and FacebookSettings.load().assignment_mode == 'shared'
    if not (is_owner or is_shared_unclaimed):
        return HttpResponseForbidden()
    if not message.media_path:
        raise Http404
    path = os.path.join(FACEBOOK_MEDIA_DIR, message.media_path)
    if not os.path.isfile(path):
        raise Http404
    return FileResponse(open(path, 'rb'), content_type=message.media_mime_type or 'application/octet-stream')


def _registered_agents_with_status():
    """Currently-registered agent endpoints (real-time "logged in" per Contact rows -- see
    ami/ami.py's ContactStatus handling) with their live call status attached. Shared by the
    admin Monitoring page and the agent-facing team-status panel on the User dashboard."""
    registered_ext_ids = Contact.objects.exclude(endpoint__isnull=True).values_list('endpoint', flat=True).distinct()
    agents = list(
        Endpoint.objects.filter(assigned_user__role='user', id__in=registered_ext_ids)
        .select_related('assigned_user').order_by('id')
    )
    queues_by_ext = {}
    for m in QueueMember.objects.all():
        ext = m.interface.split('/')[-1] if m.interface else None
        if ext:
            queues_by_ext.setdefault(ext, []).append(m.queue_name)
    current_statuses = get_current_statuses()
    for agent in agents:
        agent.queue_names = ', '.join(queues_by_ext.get(agent.id, [])) or '—'
        agent.status = current_statuses.get(agent.id, 'Unknown')
        agent.status_since = get_status_since(agent.id)
    return agents


@login_required
def admin_monitoring_view(request):
    agents = _registered_agents_with_status()
    return render(request, 'admin/monitoring.html', {'active': 'monitoring', 'agents': agents})


@login_required
def admin_force_logout_view(request, ext):
    """Kicks an agent off both the web dashboard and their phone (WebRTC or desk/softphone --
    same treatment for both, see the Contact delete below) right now, from Monitoring. Two
    independent things, both done here:
    1. Web session -- same mechanism as login_view's same-account-elsewhere kick: delete any
       active Django session for this user and push 'session_kicked' so their browser leaves
       immediately instead of only finding out on its next request.
    2. Phone registration -- delete this extension's PJSIP Contact row(s) (ps_contacts). New
       calls stop routing to/from the extension immediately; already-registered *softphone*
       apps (unlike WebRTC, which dies with the browser tab above) won't necessarily update
       their own "Registered" indicator until their next refresh cycle or a failed call
       attempt -- that's a display lag on their end, not a gap in what actually got blocked."""
    if request.method != 'POST':
        return redirect('admin_monitoring')

    endpoint = get_object_or_404(Endpoint, id=ext)
    target = endpoint.assigned_user

    if target is not None:
        from django.contrib.sessions.models import Session
        for session in Session.objects.filter(expire_date__gte=timezone.now()):
            try:
                if session.get_decoded().get('_auth_user_id') == str(target.pk):
                    session.delete()
            except Exception:
                pass

        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        channel_layer = get_channel_layer()
        if channel_layer is not None:
            async_to_sync(channel_layer.group_send)('realtime', {
                'type': 'realtime_update',
                'data': {'kind': 'session_kicked', 'user_id': target.id},
            })

    Contact.objects.filter(endpoint=ext).delete()

    label = target.full_name if target and target.full_name else ext
    messages.success(request, f'{label} has been logged out.')
    return redirect('admin_monitoring')


@login_required
def admin_leads_view(request):
    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'create_list':
            name = request.POST.get('name', '').strip()
            description = request.POST.get('description', '').strip()
            if not name:
                messages.error(request, 'Enter a name for the lead list.')
                return redirect('admin_leads')
            lead_list = LeadList.objects.create(name=name, description=description, created_by=request.user)

            numbers = _extract_lead_numbers(request)
            for number in numbers:
                Lead.objects.get_or_create(lead_list=lead_list, number=number)

            messages.success(request, f'"{name}" created with {len(numbers)} lead(s).')
            return redirect('admin_lead_list', list_id=lead_list.id)

        if action == 'delete_list':
            LeadList.objects.filter(id=request.POST.get('id')).delete()
            messages.success(request, 'Lead list removed.')
            return redirect('admin_leads')

    lead_lists = LeadList.objects.annotate(lead_count=Count('leads'))
    return render(request, 'admin/leads.html', {
        'active': 'leads', 'lead_lists': lead_lists,
    })


@login_required
def admin_lead_list_view(request, list_id):
    lead_list = get_object_or_404(LeadList, id=list_id)

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'edit_list':
            name = request.POST.get('name', '').strip()
            if not name:
                messages.error(request, 'Enter a name for the lead list.')
                return redirect('admin_lead_list', list_id=lead_list.id)
            lead_list.name = name
            lead_list.description = request.POST.get('description', '').strip()
            lead_list.save()

            numbers = _extract_lead_numbers(request)
            added = 0
            for number in numbers:
                _, created = Lead.objects.get_or_create(lead_list=lead_list, number=number)
                added += created

            messages.success(request, f'"{name}" updated, {added} new lead(s) added.')
            return redirect('admin_lead_list', list_id=lead_list.id)

        if action == 'delete':
            Lead.objects.filter(id=request.POST.get('id'), lead_list=lead_list).delete()
            messages.success(request, 'Lead removed.')
            return redirect('admin_lead_list', list_id=lead_list.id)

    leads = lead_list.leads.all()
    return render(request, 'admin/lead_list_detail.html', {
        'active': 'leads', 'lead_list': lead_list, 'leads': leads, 'total_leads': leads.count(),
    })


@login_required
def admin_settings_routing_view(request):
    form = RouteForm()
    if request.method == 'POST':
        if request.POST.get('action') == 'delete':
            name = request.POST.get('name')
            deleted, _ = Route.objects.filter(name=name).delete()
            if deleted:
                messages.success(request, f'Route "{name}" deleted.')
            return redirect('admin_settings_routing')

        if request.POST.get('action') == 'toggle':
            name = request.POST.get('name')
            route = get_object_or_404(Route, name=name)
            route.is_active = not route.is_active
            route.save(update_fields=['is_active'])
            messages.success(request, f'Route "{name}" is now {"active" if route.is_active else "inactive"}.')
            return redirect('admin_settings_routing')

        form = RouteForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, f'Route "{form.instance.name}" created.')
            return redirect('admin_settings_routing')
        # Falls through to re-render below with form.errors set, which reopens the create panel.

    trunks = []
    for t in Endpoint.objects.filter(assigned_user__isnull=True).order_by('id'):
        trunks.append({
            'id': t.id,
            'used_inbound': Route.objects.filter(trunk=t, direction='inbound').exists(),
            'used_outbound': Route.objects.filter(trunk=t, direction='outbound').exists(),
        })

    extensions = Endpoint.objects.filter(assigned_user__role='user').select_related('assigned_user').order_by('id')
    ext_names = {ext.id: ext.assigned_user.full_name for ext in extensions if ext.assigned_user and ext.assigned_user.full_name}

    inbound_routes = list(Route.objects.filter(direction='inbound').select_related('trunk').order_by('did'))
    for route in inbound_routes:
        name = ext_names.get(route.destination_id)
        route.destination_label = f'{route.destination_id} — {name}' if route.destination_type == 'extension' and name else route.destination_id

    outbound_routes = list(Route.objects.filter(direction='outbound').select_related('trunk').order_by('name'))
    for route in outbound_routes:
        name = ext_names.get(route.source_id)
        route.source_label = f'{route.source_id} — {name}' if route.source_type == 'extension' and name else route.source_id

    all_routes = inbound_routes + outbound_routes
    total_routes = len(all_routes)
    active_routes = sum(1 for r in all_routes if r.is_active)

    return render(request, 'admin/settings_routing.html', {
        'active': 'settings', 'settings_active': 'routing', 'form': form,
        'trunks': trunks,
        'extensions': extensions,
        'queues': Queue.objects.exclude(name__endswith='-opus').order_by('name'),
        'ivrs': Ivr.objects.order_by('name'),
        'inbound_routes': inbound_routes,
        'outbound_routes': outbound_routes,
        'total_routes': total_routes, 'active_routes': active_routes, 'inactive_routes': total_routes - active_routes,
    })


def _convert_sound_to_wav(uploaded_file, dest_path):
    """Any format in, 8kHz mono 16-bit WAV out -- what Asterisk's native Playback/MOH expect.
    Uploading a file already in the wrong sample rate is what silently broke MOH earlier."""
    from pydub import AudioSegment
    audio = AudioSegment.from_file(uploaded_file)
    audio = audio.set_frame_rate(8000).set_channels(1).set_sample_width(2)
    audio.export(dest_path, format='wav')


def _moh_class_folder(class_name):
    return os.path.join(MOH_DIR, class_name)


def _moh_class_files(class_name):
    folder = _moh_class_folder(class_name)
    if not os.path.isdir(folder):
        return []
    return sorted(f for f in os.listdir(folder) if not f.startswith('.'))


_MOH_PREFIX_RE = re.compile(r'^(\d{2})-(.+)$')


def _moh_track_slug(filename):
    """Strips the leading NN- play-order prefix (if any), returning the bare track slug."""
    base = os.path.splitext(filename)[0]
    m = _MOH_PREFIX_RE.match(base)
    return m.group(2) if m else base


def _next_moh_sequence(existing_files):
    """First unused NN- prefix, continuing on from whatever's already in the folder -- new
    uploads land at the end of the existing play order instead of colliding with it."""
    seqs = [int(m.group(1)) for f in existing_files for m in [_MOH_PREFIX_RE.match(f)] if m]
    return (max(seqs) + 1) if seqs else 1


def _safe_filename(filename):
    """Rejects anything that could escape the intended folder (path traversal)."""
    return bool(filename) and '/' not in filename and '..' not in filename


@login_required
def admin_settings_sounds_view(request):
    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'delete':
            filename = request.POST.get('filename')
            sound = get_object_or_404(Sound, filename=filename)
            try:
                os.remove(os.path.join(SOUNDS_DIR, f'{sound.filename}.wav'))
            except OSError:
                pass
            sound.delete()
            messages.success(request, f'Sound "{sound.name}" deleted.')
            return redirect('admin_settings_sounds')

        if action == 'delete_moh_class':
            name = request.POST.get('name')
            moh = get_object_or_404(MusicOnHold, name=name)
            shutil.rmtree(_moh_class_folder(name), ignore_errors=True)
            moh.delete()
            Queue.objects.filter(musicclass=name).update(musicclass='')
            _reload_musiconhold()
            messages.success(request, f'MOH class "{name}" deleted.')
            return redirect('admin_settings_sounds')

        if action == 'upload_moh_sound':
            class_name = request.POST.get('class_name', '').strip()
            if not class_name:
                messages.error(request, 'Class name is required.')
                return redirect('admin_settings_sounds')
            slug = slugify(class_name) or 'moh-class'
            # Files arrive in whatever order the browser sends them -- the upload panel's
            # drag-and-drop reorders the input's own FileList before submit, so this is
            # already the admin's chosen play order.
            uploads = request.FILES.getlist('file')
            if not uploads:
                messages.error(request, 'Choose at least one audio file to upload.')
                return redirect('admin_settings_sounds')
            folder = _moh_class_folder(slug)
            os.makedirs(folder, exist_ok=True)
            moh, created = MusicOnHold.objects.get_or_create(name=slug, defaults={'mode': 'files', 'directory': folder, 'sort': 'alpha'})
            if created:
                _reload_musiconhold()

            existing_files = _moh_class_files(slug)
            existing_slugs = {_moh_track_slug(f) for f in existing_files}
            next_seq = _next_moh_sequence(existing_files)

            added, skipped, failed = 0, [], []
            for uploaded in uploads:
                track_slug = slugify(os.path.splitext(uploaded.name)[0]) or 'track'
                if track_slug in existing_slugs:
                    skipped.append(uploaded.name)
                    continue
                dest = os.path.join(folder, f'{next_seq:02d}-{track_slug}.wav')
                try:
                    _convert_sound_to_wav(uploaded, dest)
                    added += 1
                    existing_slugs.add(track_slug)
                    next_seq += 1
                except Exception:
                    failed.append(uploaded.name)
            if added:
                messages.success(request, f'{added} track{"s" if added != 1 else ""} added to "{moh.name}".')
            for name in skipped:
                messages.error(request, f'"{name}" already exists in "{moh.name}" -- rename the file to upload it as a separate track.')
            for name in failed:
                messages.error(request, f'Could not convert "{name}" -- is it a valid audio file?')
            return redirect('admin_settings_sounds')

        # action == 'upload' -- regular Sound/Prompt (name derived from the uploaded filename)
        category = request.POST.get('category', 'other')
        uploaded = request.FILES.get('file')
        if not uploaded:
            messages.error(request, 'Choose an audio file to upload.')
            return redirect('admin_settings_sounds')
        name = os.path.splitext(uploaded.name)[0].replace('_', ' ').replace('-', ' ').strip().title() or 'Sound'
        if Sound.objects.filter(name=name).exists():
            messages.error(request, f'A sound named "{name}" already exists -- rename the file before uploading, or delete the existing one first.')
            return redirect('admin_settings_sounds')

        os.makedirs(SOUNDS_DIR, exist_ok=True)
        slug = slugify(name) or 'sound'
        unique_slug, n = slug, 1
        # Checked against both the DB and the filesystem -- /var/lib/asterisk/sounds/ already
        # has real Asterisk prompt files (welcome.wav, after_hour.wav, ...) that aren't Sound
        # rows, so DB-only uniqueness could silently overwrite one of those.
        while Sound.objects.filter(filename=unique_slug).exists() or os.path.exists(os.path.join(SOUNDS_DIR, f'{unique_slug}.wav')):
            n += 1
            unique_slug = f'{slug}-{n}'

        try:
            _convert_sound_to_wav(uploaded, os.path.join(SOUNDS_DIR, f'{unique_slug}.wav'))
        except Exception:
            messages.error(request, f'Could not convert "{uploaded.name}" -- is it a valid audio file?')
            return redirect('admin_settings_sounds')

        Sound.objects.create(name=name, category=category, filename=unique_slug)
        messages.success(request, f'Sound "{name}" uploaded.')
        return redirect('admin_settings_sounds')

    sounds = Sound.objects.all().order_by('category', 'name')
    moh_classes = [
        {'name': moh.name, 'description': moh.description, 'files': _moh_class_files(moh.name)}
        for moh in MusicOnHold.objects.order_by('name')
    ]

    return render(request, 'admin/settings_sounds.html', {
        'active': 'settings', 'settings_active': 'sounds', 'sounds': sounds,
        'moh_classes': moh_classes,
    })


@login_required
def admin_settings_sound_preview_view(request, filename):
    sound = get_object_or_404(Sound, filename=filename)
    path = os.path.join(SOUNDS_DIR, f'{sound.filename}.wav')
    if not os.path.exists(path):
        raise Http404
    return FileResponse(open(path, 'rb'), content_type='audio/wav')


@login_required
def admin_settings_moh_track_preview_view(request, class_name, filename):
    if not _safe_filename(filename):
        raise Http404
    path = os.path.join(_moh_class_folder(class_name), filename)
    if not os.path.exists(path):
        raise Http404
    return FileResponse(open(path, 'rb'), content_type='audio/wav')


def _my_calls_context(request, endpoint_ids):
    search = request.GET.get('mc_search', '').strip()
    date_from = request.GET.get('mc_date_from', '').strip()
    date_to = request.GET.get('mc_date_to', '').strip()

    # No range given -- default to today's data; older days are reachable via the date filter.
    if not date_from and not date_to:
        date_from = date_to = _local_today().isoformat()

    # Inbound queue calls' dst is the DID, never this agent's own extension (see
    # _cdr_agent_endpoint_id) -- the extension that actually answered only shows up in
    # dstchannel, so match on that instead of dst for the inbound side.
    own_calls = Q(dcontext='from-internal', src__in=endpoint_ids)
    if endpoint_ids:
        answered_q = Q()
        for eid in endpoint_ids:
            answered_q |= Q(dstchannel__startswith=f'PJSIP/{eid}-')
        own_calls |= Q(dcontext__in=INBOUND_TRUNK_CONTEXTS) & answered_q
    calls = _canonical_cdrs(Cdr.objects.filter(own_calls))
    if search:
        calls = calls.filter(Q(src__icontains=search) | Q(dst__icontains=search))
    if date_from:
        try:
            calls = calls.filter(start__date__gte=datetime.date.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            calls = calls.filter(start__date__lte=datetime.date.fromisoformat(date_to))
        except ValueError:
            pass
    calls = calls.order_by('-start')

    paginator = Paginator(calls, 25)
    page_obj = paginator.get_page(request.GET.get('mc_page'))

    def fmt_duration(seconds):
        seconds = int(seconds or 0)
        return f'{seconds // 60}m {seconds % 60:02d}s'

    rows = []
    for row in page_obj:
        if row.dcontext == 'from-internal':
            # See admin_cdr_view's identical check -- an Auto Dial lead the agent's own leg
            # never answered never reaches the real dst (Asterisk logs 's' instead); the real
            # number is always in userfield regardless.
            auto_dial_parties = _auto_dial_cdr_parties(row.userfield)
            direction, number = 'Outbound', auto_dial_parties[1] if auto_dial_parties else row.dst
        elif row.dcontext in INBOUND_TRUNK_CONTEXTS:
            direction, number = 'Inbound', row.src
        else:
            direction, number = row.dcontext or '—', row.src or row.dst
        rows.append({
            'number': number, 'direction': direction, 'duration_display': fmt_duration(row.billsec),
            'disposition': row.disposition, 'start': row.start,
        })

    querystring = request.GET.copy()
    querystring.pop('mc_page', None)

    return {
        'my_calls_rows': rows, 'my_calls_page': page_obj,
        'mc_search': search, 'mc_date_from': date_from, 'mc_date_to': date_to,
        'mc_querystring': querystring.urlencode(),
    }


def _recent_dropped_calls():
    """All queue ABANDON events not yet called back (see DropCall) -- no date/count limit, an
    entry only leaves this list once someone marks it handled. Caller's number is filled in from
    CDR (data3 on ABANDON is the wait time -- see _QUEUE_LOG_FIELD_LABELS -- it doesn't carry
    the number)."""
    rows = list(
        QueueLog.objects.filter(event='ABANDON')
        .exclude(callid__in=DropCall.objects.values('callid'))
        .order_by('-time')
    )
    callids = [r.callid for r in rows if r.callid]
    numbers_by_callid = dict(
        Cdr.objects.filter(uniqueid__in=callids).values_list('uniqueid', 'src')
    )
    results = []
    for r in rows:
        results.append({
            'callid': r.callid,
            'time': _queue_log_time_display(r.time),
            'number': numbers_by_callid.get(r.callid) or 'Unknown',
            'queue': r.queuename,
            'wait_seconds': _report_int(r.data3) or 0,
        })
    return results


@login_required
def user_mark_dropcall_view(request):
    """Callback button on the Drop Call panel -- marks the abandoned call as handled so it drops
    out of everyone's list (see _recent_dropped_calls), and broadcasts that removal live to every
    open dashboard over the same 'realtime' Channels group the AMI listener uses."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    callid = request.POST.get('callid', '').strip()
    if not callid:
        return JsonResponse({'error': 'Invalid request'}, status=400)

    DropCall.objects.get_or_create(callid=callid, defaults={'called_by': request.user})

    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    channel_layer = get_channel_layer()
    if channel_layer is not None:
        async_to_sync(channel_layer.group_send)('realtime', {
            'type': 'realtime_update',
            'data': {'kind': 'drop_call_resolved', 'callid': callid},
        })
    return JsonResponse({'success': True})


@login_required
async def user_toggle_pause_view(request):
    """Ready/Paused toggle on the WebRTC status bar -- actually pauses the agent in Asterisk's
    queue (not just a UI label) via AMI, so no new calls route to them while paused. Must be
    async: it talks to the same panoramisk Manager instance the AMI listener owns, which is not
    thread/loop-safe, so this has to run on that same event loop."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    user = await request.auser()
    paused = request.POST.get('paused') == 'true'
    endpoint = await Endpoint.objects.filter(assigned_user=user).afirst()
    if not endpoint:
        return JsonResponse({'error': 'No extension assigned to this account.'}, status=400)

    from ami.ami import set_pause
    await set_pause(endpoint.id, paused)
    return JsonResponse({'success': True, 'paused': paused})


@login_required
async def user_transfer_view(request):
    """Transfer button on the WebRTC widget -- see ami.transfer_call for the actual AMI
    orchestration (ring the target as a separate leg, only swap the customer onto it if/when
    target answers). Async for the same reason as user_toggle_pause_view: talks to the AMI
    listener's own panoramisk Manager, which must run on that same event loop."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    user = await request.auser()
    target = request.POST.get('target', '').strip()
    if not target:
        return JsonResponse({'error': 'No transfer target given.'}, status=400)
    endpoint = await Endpoint.objects.filter(assigned_user=user).afirst()
    if not endpoint:
        return JsonResponse({'error': 'No extension assigned to this account.'}, status=400)

    from ami.ami import transfer_call
    result = await transfer_call(endpoint.id, target)
    return JsonResponse(result)


def _today_call_stats(endpoint_ids):
    """Total Calls/Inbound/Outbound/Talk Time for the given extensions, today. Shared by the
    User dashboard's initial render and the AMI-driven live update (see ami/ami.py's Hangup
    handler) so both compute the numbers exactly the same way."""
    today = _local_today()
    todays_cdrs = _canonical_cdrs(Cdr.objects.filter(start__date=today))
    # Inbound queue calls' dst is the DID, never this agent's own extension (see
    # _cdr_agent_endpoint_id) -- match on dstchannel (who actually answered) instead.
    answered_q = Q()
    for eid in endpoint_ids:
        answered_q |= Q(dstchannel__startswith=f'PJSIP/{eid}-')
    inbound_cdrs = todays_cdrs.filter(Q(dcontext__in=INBOUND_TRUNK_CONTEXTS) & answered_q) if endpoint_ids else todays_cdrs.none()
    outbound_cdrs = todays_cdrs.filter(dcontext='from-internal', src__in=endpoint_ids)
    total_inbound = inbound_cdrs.count()
    total_outbound = outbound_cdrs.count()

    def fmt_hms(seconds):
        return f'{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}'

    inbound_talk_seconds = inbound_cdrs.aggregate(s=Sum('billsec'))['s'] or 0
    outbound_talk_seconds = outbound_cdrs.aggregate(s=Sum('billsec'))['s'] or 0
    total_talk_seconds = inbound_talk_seconds + outbound_talk_seconds
    return {
        'total_calls_today': total_inbound + total_outbound,
        'total_inbound': total_inbound,
        'total_outbound': total_outbound,
        'total_inbound_talk_time': fmt_hms(inbound_talk_seconds),
        'total_outbound_talk_time': fmt_hms(outbound_talk_seconds),
        'total_talk_time': fmt_hms(total_talk_seconds),
    }


@login_required
def user_conversation_thread_view(request):
    """AJAX endpoint backing selectWhatsappConversation()/selectFacebookConversation() (see
    user/whatsapp.html, user/facebook.html) -- returns the selected conversation's thread pane
    as a rendered HTML fragment, so opening a chat never navigates the browser (which would tear
    down and reconnect the embedded WebRTC widget shared across every tab on this one page)."""
    channel = request.GET.get('channel')
    conv_id = request.GET.get('id', '').strip()
    if channel == 'whatsapp':
        conv_filter = Q(assigned_user=request.user)
        if WhatsAppSettings.load().assignment_mode == 'shared':
            conv_filter |= Q(assigned_user__isnull=True)
        _, selected, thread = _whatsapp_conversations_context(
            WhatsAppConversation.objects.filter(conv_filter), conv_id
        )
        if not selected or selected.number != conv_id:
            return JsonResponse({'error': 'not found'}, status=404)
        html = render_to_string('user/_whatsapp_thread.html', {
            'whatsapp_selected': selected, 'whatsapp_thread': thread,
        }, request=request)
    elif channel == 'facebook':
        conv_filter = Q(assigned_user=request.user)
        if FacebookSettings.load().assignment_mode == 'shared':
            conv_filter |= Q(assigned_user__isnull=True)
        _, selected, thread = _facebook_conversations_context(
            FacebookConversation.objects.filter(conv_filter), conv_id
        )
        if not selected or selected.psid != conv_id:
            return JsonResponse({'error': 'not found'}, status=404)
        html = render_to_string('user/_facebook_thread.html', {
            'facebook_selected': selected, 'facebook_thread': thread,
        }, request=request)
    else:
        return JsonResponse({'error': 'invalid channel'}, status=400)
    return JsonResponse({'html': html})


@login_required
def user_dashboard_view(request):
    if request.method == 'POST':
        if request.POST.get('form') == 'crm':
            _crm_handle_post(request)
            return redirect(f"{reverse('user_dashboard')}?tab=crm")
        if request.POST.get('action') in ('send', 'send_media', 'mark_read', 'typing'):
            if 'psid' in request.POST:
                return _facebook_handle_agent_action(request)
            return _whatsapp_handle_agent_action(request)

    endpoint_ids = list(Endpoint.objects.filter(assigned_user=request.user).values_list('id', flat=True))
    # 'shared' mode: an unassigned conversation is unclaimed shared-inbox territory every agent
    # sees, until whoever replies first claims it (see _whatsapp_handle_agent_action). Every
    # other mode only ever assigns straight to one agent, so nothing here is ever unassigned by
    # design -- except a stray leftover from 'manual'/'shared' being on at assignment time, in
    # which case it's still fine for it to just not show up here (the admin report handles it).
    whatsapp_conv_filter = Q(assigned_user=request.user)
    if WhatsAppSettings.load().assignment_mode == 'shared':
        whatsapp_conv_filter |= Q(assigned_user__isnull=True)
    whatsapp_conversations, whatsapp_selected, whatsapp_thread = _whatsapp_conversations_context(
        WhatsAppConversation.objects.filter(whatsapp_conv_filter), request.GET.get('number')
    )

    facebook_conv_filter = Q(assigned_user=request.user)
    if FacebookSettings.load().assignment_mode == 'shared':
        facebook_conv_filter |= Q(assigned_user__isnull=True)
    facebook_conversations, facebook_selected, facebook_thread = _facebook_conversations_context(
        FacebookConversation.objects.filter(facebook_conv_filter), request.GET.get('psid')
    )

    context = {
        'form': CrmForm(),
        'my_extensions': endpoint_ids,
        **_today_call_stats(endpoint_ids),
        'team_status': _registered_agents_with_status(),
        'live_queue_callers': get_live_queue_callers(),
        'dropped_calls': _recent_dropped_calls(),
        'whatsapp_conversations': whatsapp_conversations,
        'whatsapp_selected': whatsapp_selected,
        'whatsapp_thread': whatsapp_thread,
        'facebook_conversations': facebook_conversations,
        'facebook_selected': facebook_selected,
        'facebook_thread': facebook_thread,
        **_my_calls_context(request, endpoint_ids),
    }
    return render(request, 'user/panel.html', context)


