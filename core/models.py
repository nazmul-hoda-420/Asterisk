from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    ROLE_CHOICES = [
        ('user', 'User'),
        ('supervisor', 'Supervisor'),
        ('admin', 'Admin'),
    ]

    full_name = models.CharField(max_length=255)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='user')
    plain_password = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        db_table = 'user'


class Activity(models.Model):
    """One row per web-dashboard login/logout -- powers Reports -> Activity's First
    Login/Last Logout/Total Login Time. Deliberately independent of PJSIP registration: a
    phone/softphone/WebRTC contact can stay registered for days without a fresh dashboard
    login, so QueueLog's ADDMEMBER/REMOVEMEMBER (queue-membership side effects of that
    registration) don't reflect "logged in today" the way this does. login_view and
    logout_view are the only writers; logout_at stays null while the session is still open."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sessions')
    login_at = models.DateTimeField(auto_now_add=True)
    logout_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'activity'
        ordering = ['-login_at']


YES_NO_CHOICES = [('yes', 'Yes'), ('no', 'No')]


class Auth(models.Model):
    """PJSIP realtime auth objects (SIP credentials). Read by Asterisk via ODBC — see extconfig.conf."""
    id = models.CharField(max_length=40, primary_key=True)
    auth_type = models.CharField(max_length=40, choices=[('userpass', 'Username/Password'), ('md5', 'MD5')], default='userpass')
    username = models.CharField(max_length=40, blank=True, null=True)
    password = models.CharField(max_length=80, blank=True, null=True)
    realm = models.CharField(max_length=40, blank=True, null=True)

    class Meta:
        db_table = 'ps_auths'

    def __str__(self):
        return self.id


class Aor(models.Model):
    """PJSIP realtime AOR objects — where an endpoint's registered contacts are stored."""
    id = models.CharField(max_length=40, primary_key=True)
    contact = models.CharField(max_length=255, blank=True, null=True)
    default_expiration = models.IntegerField(blank=True, null=True)
    mailboxes = models.CharField(max_length=40, blank=True, null=True)
    max_contacts = models.IntegerField(default=1)
    minimum_expiration = models.IntegerField(blank=True, null=True)
    remove_existing = models.CharField(max_length=3, choices=YES_NO_CHOICES, default='yes')
    qualify_frequency = models.IntegerField(blank=True, null=True)
    authenticate_qualify = models.CharField(max_length=10, choices=YES_NO_CHOICES, blank=True, null=True)
    support_path = models.CharField(max_length=3, choices=YES_NO_CHOICES, blank=True, null=True)

    class Meta:
        db_table = 'ps_aors'

    def __str__(self):
        return self.id


class Endpoint(models.Model):
    """PJSIP realtime endpoint objects — one row per SIP extension. Powers Settings → Extension."""
    id = models.CharField(max_length=40, primary_key=True)
    transport = models.CharField(max_length=40, blank=True, null=True)
    aors = models.CharField(max_length=200, blank=True, null=True)
    auth = models.CharField(max_length=100, blank=True, null=True)
    outbound_auth = models.CharField(max_length=100, blank=True, null=True)
    context = models.CharField(max_length=40, default='from-internal')
    disallow = models.CharField(max_length=200, default='all')
    allow = models.CharField(max_length=200, default='ulaw,alaw')
    direct_media = models.CharField(max_length=3, choices=YES_NO_CHOICES, default='no')
    dtmf_mode = models.CharField(max_length=20, choices=[
        ('rfc4733', 'RFC4733'), ('inband', 'Inband'), ('info', 'SIP INFO'), ('auto', 'Auto'),
    ], default='rfc4733')
    callerid = models.CharField(max_length=100, blank=True, null=True)
    trust_id_inbound = models.CharField(max_length=3, choices=YES_NO_CHOICES, default='no')
    mailboxes = models.CharField(max_length=40, blank=True, null=True)
    force_rport = models.CharField(max_length=3, choices=YES_NO_CHOICES, default='yes')
    rewrite_contact = models.CharField(max_length=3, choices=YES_NO_CHOICES, default='yes')
    rtp_symmetric = models.CharField(max_length=3, choices=YES_NO_CHOICES, default='yes')
    ice_support = models.CharField(max_length=3, choices=YES_NO_CHOICES, default='no')
    use_avpf = models.CharField(max_length=3, choices=YES_NO_CHOICES, default='no')
    media_encryption = models.CharField(max_length=10, choices=[
        ('no', 'None'), ('sdes', 'SDES'), ('dtls', 'DTLS (WebRTC)'),
    ], default='no')
    rtcp_mux = models.CharField(max_length=3, choices=YES_NO_CHOICES, default='no')
    webrtc = models.CharField(max_length=3, choices=YES_NO_CHOICES, blank=True, null=True)
    dtls_auto_generate_cert = models.CharField(max_length=3, choices=YES_NO_CHOICES, blank=True, null=True)
    # Asterisk's own default (actpass) is only valid in an SDP *offer* -- an *answer* must
    # commit to active/passive (RFC 5763), so a UAS-only endpoint (like a WhatsApp Calling
    # trunk, which only ever answers Meta's inbound INVITEs) needs this pinned to 'active' or
    # the far end gets an invalid answer, never ACKs it, and the call self-destructs after
    # ~32s (64*T1) with no BYE. See WhatsApp trunk creation in views.py.
    dtls_setup = models.CharField(max_length=10, choices=[
        ('active', 'Active'), ('passive', 'Passive'), ('actpass', 'Act/Pass'),
    ], blank=True, null=True)
    device_state_busy_at = models.IntegerField(blank=True, null=True)

    # Not part of Asterisk's PJSIP schema — Asterisk's realtime engine ignores columns
    # it doesn't recognize, so it's safe to track our own app-level link here.
    assigned_user = models.ForeignKey(
        'User', null=True, blank=True, on_delete=models.SET_NULL, related_name='pjsip_endpoints'
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'ps_endpoints'

    def __str__(self):
        return self.id


class EndpointIdIp(models.Model):
    """PJSIP realtime identify objects — matches inbound requests to an endpoint by source IP
    (used for trunks/providers, see Settings → Registration; `match` takes a comma-separated
    list of IPs/networks, so one row can cover a whole provider's range, e.g. Meta's AS32934
    for WhatsApp Business Calling -- see wa_business_identify). match_header (matching a SIP
    header's value instead of source IP, meant for peers without a static IP) is present in
    this Asterisk 23.2.2 build's schema but doesn't actually work -- confirmed by testing:
    identical auth/endpoint/aor validated fine once switched to IP-based match, but always
    failed digest auth when matched via match_header. Kept only for schema compatibility;
    don't rely on it."""
    id = models.CharField(max_length=40, primary_key=True)
    endpoint = models.CharField(max_length=40, blank=True, null=True)
    match = models.TextField(blank=True, null=True)
    match_header = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        db_table = 'ps_endpoint_id_ips'

    def __str__(self):
        return self.id


class Registration(models.Model):
    """PJSIP realtime outbound-registration objects — makes Asterisk send periodic REGISTER
    requests to a provider that requires username/password auth (see Settings → Registration).
    Providers that trust by source IP instead don't need a row here — see EndpointIdIp."""
    id = models.CharField(max_length=40, primary_key=True)
    server_uri = models.CharField(max_length=255, blank=True, null=True)
    client_uri = models.CharField(max_length=255, blank=True, null=True)
    contact_user = models.CharField(max_length=80, blank=True, null=True)
    transport = models.CharField(max_length=40, blank=True, null=True)
    outbound_auth = models.CharField(max_length=100, blank=True, null=True)
    outbound_proxy = models.CharField(max_length=255, blank=True, null=True)
    expiration = models.IntegerField(default=3600, blank=True, null=True)
    retry_interval = models.IntegerField(default=60, blank=True, null=True)
    max_retries = models.IntegerField(default=10, blank=True, null=True)
    forbidden_retry_interval = models.IntegerField(blank=True, null=True)
    fatal_retry_interval = models.IntegerField(blank=True, null=True)
    max_random_initial_delay = models.IntegerField(blank=True, null=True)
    auth_rejection_permanent = models.CharField(max_length=10, choices=YES_NO_CHOICES, blank=True, null=True)
    support_path = models.CharField(max_length=10, choices=YES_NO_CHOICES, blank=True, null=True)
    line = models.CharField(max_length=10, choices=YES_NO_CHOICES, blank=True, null=True)
    endpoint = models.CharField(max_length=40, blank=True, null=True)

    class Meta:
        db_table = 'ps_registrations'

    def __str__(self):
        return self.id


class Contact(models.Model):
    """PJSIP realtime contact objects — populated automatically by Asterisk when a phone/softphone registers (this app never writes to it directly)."""
    id = models.CharField(max_length=255, primary_key=True)
    uri = models.CharField(max_length=511, blank=True, null=True)
    endpoint = models.CharField(max_length=40, blank=True, null=True)
    expiration_time = models.BigIntegerField(blank=True, null=True)
    qualify_frequency = models.IntegerField(blank=True, null=True)
    outbound_proxy = models.CharField(max_length=255, blank=True, null=True)
    path = models.TextField(blank=True, null=True)
    user_agent = models.CharField(max_length=255, blank=True, null=True)
    qualify_timeout = models.DecimalField(max_digits=8, decimal_places=3, blank=True, null=True)
    reg_server = models.CharField(max_length=255, blank=True, null=True)
    authenticate_qualify = models.CharField(max_length=10, choices=YES_NO_CHOICES, blank=True, null=True)
    via_addr = models.CharField(max_length=255, blank=True, null=True)
    via_port = models.IntegerField(blank=True, null=True)
    call_id = models.CharField(max_length=255, blank=True, null=True)
    prune_on_boot = models.CharField(max_length=10, choices=YES_NO_CHOICES, blank=True, null=True)
    qualify_2xx_only = models.CharField(max_length=10, choices=YES_NO_CHOICES, blank=True, null=True)

    class Meta:
        db_table = 'ps_contacts'

    def __str__(self):
        return self.id


QUEUE_STRATEGY_CHOICES = [
    ('ringall', 'Ring All'),
    ('leastrecent', 'Least Recent'),
    ('fewestcalls', 'Fewest Calls'),
    ('random', 'Random'),
    ('rrmemory', 'Round Robin (Memory)'),
    ('linear', 'Linear'),
    ('wrandom', 'Weighted Random'),
]


class Queue(models.Model):
    """Asterisk realtime queue — distributes inbound calls to a set of agent members
    according to a ring strategy. Powers Settings → Queue."""
    name = models.CharField(max_length=128, primary_key=True)

    # Not part of Asterisk's queues.conf/realtime schema — app-level only, same precedent as
    # Endpoint.assigned_user (Asterisk's realtime engine ignores columns it doesn't recognize).
    description = models.CharField(max_length=255, blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # A MusicOnHold class name (Settings -> Sounds manages the classes and their tracks).
    musicclass = models.CharField(max_length=80, blank=True, null=True)

    # Not part of Asterisk's queues.conf/realtime schema -- app-level only, same precedent as
    # description/is_active above. Picked from the Sound library (Settings -> Sounds).
    welcome_sound = models.ForeignKey(
        'Sound', blank=True, null=True, on_delete=models.SET_NULL, related_name='+',
        limit_choices_to={'category': 'welcome'},
        help_text='Played before connecting the caller during business hours',
    )
    after_hours_sound = models.ForeignKey(
        'Sound', blank=True, null=True, on_delete=models.SET_NULL, related_name='+',
        limit_choices_to={'category': 'after_hours'},
        help_text='Played instead of the welcome sound outside business hours',
    )
    # A named schedule from Settings -> Call Time. If unset, the welcome sound always plays
    # and after-hours is never used.
    call_time = models.ForeignKey(
        'CallTime', blank=True, null=True, on_delete=models.SET_NULL, related_name='queues',
        help_text='Business-hours schedule that decides Welcome vs After Hours',
    )

    strategy = models.CharField(max_length=20, choices=QUEUE_STRATEGY_CHOICES, default='ringall')
    timeout = models.IntegerField(default=15, help_text='Seconds to ring each member before trying the next')
    retry = models.IntegerField(default=5, help_text='Seconds to wait before retrying members after a full round')
    wrapuptime = models.IntegerField(default=0, help_text='Seconds a member rests after a call before getting a new one')
    maxlen = models.IntegerField(default=0, help_text='Max callers allowed to wait (0 = unlimited)')
    servicelevel = models.IntegerField(default=60, help_text='Target answer time in seconds, used for reporting')
    weight = models.IntegerField(default=0, help_text='Priority when a member belongs to multiple queues — higher wins ties')
    ringinuse = models.CharField(max_length=10, choices=YES_NO_CHOICES, default='yes', help_text='Ring members who are already on a call')
    autofill = models.CharField(max_length=10, choices=YES_NO_CHOICES, default='yes')
    autopause = models.CharField(max_length=10, choices=[('yes', 'Yes'), ('no', 'No'), ('all', 'All')], default='no', help_text='Auto-pause a member after they miss a call')
    reportholdtime = models.CharField(max_length=10, choices=YES_NO_CHOICES, default='no')
    timeoutrestart = models.CharField(max_length=10, choices=YES_NO_CHOICES, default='no')
    joinempty = models.CharField(max_length=255, blank=True, default='', help_text="Conditions that block joining when no agent is available, e.g. paused,invalid")
    leavewhenempty = models.CharField(max_length=255, blank=True, default='', help_text="Conditions that eject waiting callers, e.g. paused,invalid")
    context = models.CharField(max_length=80, blank=True, null=True, help_text='Dialplan context a caller can escape to by pressing a digit while waiting')
    announce = models.CharField(max_length=255, blank=True, null=True, help_text="Sound file played to the member before connecting the call")
    periodic_announce = models.CharField(max_length=255, blank=True, null=True)
    periodic_announce_frequency = models.IntegerField(blank=True, null=True, help_text='Seconds between periodic announcements')
    announce_frequency = models.IntegerField(blank=True, null=True, help_text='Seconds between position/hold-time announcements to the caller')
    announce_holdtime = models.CharField(max_length=10, choices=[('yes', 'Yes'), ('no', 'No'), ('once', 'Once')], blank=True, null=True)
    announce_position = models.CharField(max_length=20, choices=[('yes', 'Yes'), ('no', 'No'), ('limit', 'Limit'), ('more', 'More')], blank=True, null=True)
    monitor_format = models.CharField(max_length=20, blank=True, null=True)
    membermacro = models.CharField(max_length=80, blank=True, null=True)
    membergosub = models.CharField(max_length=80, blank=True, null=True)
    setinterfacevar = models.CharField(max_length=10, choices=YES_NO_CHOICES, blank=True, null=True)
    setqueuevar = models.CharField(max_length=10, choices=YES_NO_CHOICES, blank=True, null=True)
    setqueueentryvar = models.CharField(max_length=10, choices=YES_NO_CHOICES, blank=True, null=True)
    eventmemberstatus = models.CharField(max_length=10, choices=YES_NO_CHOICES, blank=True, null=True)
    eventwhencalled = models.CharField(max_length=10, choices=YES_NO_CHOICES, blank=True, null=True)
    memberdelay = models.IntegerField(blank=True, null=True)

    class Meta:
        db_table = 'queues'

    def __str__(self):
        return self.name


class QueueMember(models.Model):
    """Realtime queue membership rows — which extensions belong to which queue, and their
    per-member call-distribution settings."""
    uniqueid = models.AutoField(primary_key=True)
    queue_name = models.CharField(max_length=128)
    interface = models.CharField(max_length=128, help_text='e.g. PJSIP/1001')
    membername = models.CharField(max_length=80, blank=True, null=True)
    state_interface = models.CharField(max_length=128, blank=True, null=True)
    penalty = models.IntegerField(default=0, help_text='Lower rings sooner relative to other members')
    paused = models.IntegerField(default=0)
    ringinuse = models.CharField(max_length=10, choices=YES_NO_CHOICES, blank=True, null=True)
    wrapuptime = models.IntegerField(blank=True, null=True)

    class Meta:
        db_table = 'queue_members'

    def __str__(self):
        return f'{self.queue_name}: {self.interface}'


class QueueLog(models.Model):
    """Realtime queue event log — Asterisk writes here automatically (join, abandon, connect,
    complete, pause, etc.) whenever a queue event happens. This app never writes to it; the
    table just needs to exist so Asterisk's writes succeed, and it can power a future queue
    reporting page."""
    id = models.AutoField(primary_key=True)
    time = models.CharField(max_length=26, blank=True, null=True, db_index=True)
    callid = models.CharField(max_length=32, blank=True, null=True, db_index=True)
    queuename = models.CharField(max_length=128, blank=True, null=True, db_index=True)
    agent = models.CharField(max_length=80, blank=True, null=True)
    event = models.CharField(max_length=32, blank=True, null=True, db_index=True)
    data1 = models.CharField(max_length=255, blank=True, null=True)
    data2 = models.CharField(max_length=255, blank=True, null=True)
    data3 = models.CharField(max_length=255, blank=True, null=True)
    data4 = models.CharField(max_length=255, blank=True, null=True)
    data5 = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        db_table = 'queue_log'

    def __str__(self):
        return f'{self.time} {self.queuename} {self.event}'


class Cdr(models.Model):
    """Call Detail Records -- one row per call leg, written automatically by Asterisk's
    cdr_adaptive_odbc backend (see /etc/asterisk/cdr_adaptive_odbc.conf). This app never writes
    here; the table just needs to exist, with column names matching what Asterisk sends, for
    those writes to succeed. Powers Settings -> CDR and the Reports dashboard.

    Asterisk's internal CDR field is called "end", not a usable Postgres column name here, so
    cdr_adaptive_odbc.conf aliases it to end_time -- see `alias end => end_time` in that file."""
    # All fields below are nullable, no defaults -- Asterisk's adaptive ODBC backend sends a
    # sparse INSERT (only columns it has a value for on a given call leg), and a Django-level
    # `default` isn't a real SQL DEFAULT, so any NOT NULL column Asterisk happens to omit would
    # fail the insert outright (this broke CDR logging entirely until traced to this).
    id = models.BigAutoField(primary_key=True)
    accountcode = models.CharField(max_length=20, blank=True, null=True)
    src = models.CharField(max_length=80, blank=True, null=True, db_index=True)
    dst = models.CharField(max_length=80, blank=True, null=True, db_index=True)
    dcontext = models.CharField(max_length=80, blank=True, null=True)
    clid = models.CharField(max_length=80, blank=True, null=True)
    channel = models.CharField(max_length=80, blank=True, null=True)
    dstchannel = models.CharField(max_length=80, blank=True, null=True)
    lastapp = models.CharField(max_length=80, blank=True, null=True)
    lastdata = models.CharField(max_length=80, blank=True, null=True)
    start = models.DateTimeField(null=True, blank=True, db_index=True)
    answer = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    duration = models.IntegerField(null=True, blank=True, help_text='Seconds from start to end')
    billsec = models.IntegerField(null=True, blank=True, help_text='Seconds from answer to end (talk time)')
    disposition = models.CharField(max_length=45, blank=True, null=True, db_index=True, help_text='ANSWERED, NO ANSWER, BUSY, FAILED, CONGESTION')
    amaflags = models.IntegerField(null=True, blank=True)
    uniqueid = models.CharField(max_length=150, blank=True, null=True, db_index=True)
    userfield = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    linkedid = models.CharField(max_length=150, blank=True, null=True, db_index=True)

    class Meta:
        db_table = 'cdr'

    def __str__(self):
        return f'{self.src} -> {self.dst} ({self.start})'


class Cel(models.Model):
    """Channel Event Log -- a finer-grained event stream than Cdr (channel start/end, answer,
    app start/end, bridge start/end, transfer, ...), written automatically by Asterisk's
    cel_odbc backend (see /etc/asterisk/cel_odbc.conf, [asterisk] context). This app never
    writes here; the table just needs to exist, with column names matching what Asterisk sends.
    Mostly useful for technical drill-down (Settings -> CEL); Reports is built from Cdr/QueueLog
    instead since CEL is too granular for a summary dashboard."""
    # See the comment on Cdr above -- same reasoning, all nullable, no defaults.
    id = models.BigAutoField(primary_key=True)
    eventtype = models.CharField(max_length=30, blank=True, null=True, db_index=True)
    eventtime = models.DateTimeField(null=True, blank=True, db_index=True)
    userdeftype = models.CharField(max_length=30, blank=True, null=True)
    cid_name = models.CharField(max_length=80, blank=True, null=True)
    cid_num = models.CharField(max_length=80, blank=True, null=True)
    cid_ani = models.CharField(max_length=80, blank=True, null=True)
    cid_rdnis = models.CharField(max_length=80, blank=True, null=True)
    cid_dnid = models.CharField(max_length=80, blank=True, null=True)
    exten = models.CharField(max_length=80, blank=True, null=True)
    context = models.CharField(max_length=80, blank=True, null=True)
    channame = models.CharField(max_length=80, blank=True, null=True)
    appname = models.CharField(max_length=80, blank=True, null=True)
    appdata = models.CharField(max_length=160, blank=True, null=True)
    accountcode = models.CharField(max_length=20, blank=True, null=True)
    peeraccount = models.CharField(max_length=20, blank=True, null=True)
    uniqueid = models.CharField(max_length=150, blank=True, null=True, db_index=True)
    linkedid = models.CharField(max_length=150, blank=True, null=True, db_index=True)
    amaflags = models.IntegerField(null=True, blank=True)
    userfield = models.CharField(max_length=255, blank=True, null=True)
    peer = models.CharField(max_length=80, blank=True, null=True)
    extra = models.TextField(blank=True, null=True, help_text='Event-specific JSON payload Asterisk attaches to some CEL events')

    class Meta:
        db_table = 'cel'

    def __str__(self):
        return f'{self.eventtype} {self.channame} ({self.eventtime})'


# Sentinel stored in Route.source_id for an outbound route with no source filter selected in
# the form (a catch-all/fallback route) — an empty string can't be used for this because
# inbound routes also leave source_id blank, and REALTIME_FIELD's single-field match would
# then ambiguously hit either kind of row.
ROUTE_SOURCE_ANY = 'any'


class Route(models.Model):
    """App-defined realtime family (not an Asterisk-native table) backing Settings → Routing.
    extensions.conf resolves both directions from this table live, per call, via
    REALTIME_FIELD() — see core/views.py's dialplan comments for the exact lookup chain."""
    DIRECTION_CHOICES = [('inbound', 'Inbound'), ('outbound', 'Outbound')]
    DESTINATION_TYPE_CHOICES = [('extension', 'Extension'), ('queue', 'Queue'), ('ivr', 'IVR')]
    SOURCE_TYPE_CHOICES = [('extension', 'Extension'), ('queue', 'Queue')]

    name = models.CharField(max_length=80, primary_key=True)
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES)
    trunk = models.ForeignKey(
        'Endpoint', on_delete=models.CASCADE, related_name='routes',
        limit_choices_to={'assigned_user__isnull': True},
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Inbound only: which dialed DID lands here, and where it goes.
    did = models.CharField(max_length=40, blank=True, default='')
    destination_type = models.CharField(max_length=10, choices=DESTINATION_TYPE_CHOICES, blank=True, default='')
    destination_id = models.CharField(max_length=80, blank=True, default='')

    # Outbound only: who may dial out through this trunk. ROUTE_SOURCE_ANY = catch-all.
    source_type = models.CharField(max_length=10, choices=SOURCE_TYPE_CHOICES, blank=True, default='')
    source_id = models.CharField(max_length=80, blank=True, default='')

    class Meta:
        db_table = 'routes'

    def __str__(self):
        return self.name


class Crm(models.Model):
    """A customer/lead record. Call history and "last contact" aren't stored here -- they're
    looked up live from Cdr by matching this contact's phone against Cdr.src/Cdr.dst, so they
    never go stale."""
    TAG_CHOICES = [('lead', 'Lead'), ('customer', 'Customer'), ('vip', 'VIP')]

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=150)
    phone = models.CharField(max_length=40, unique=True)
    email = models.EmailField(max_length=255, blank=True, default='')
    tag = models.CharField(max_length=10, choices=TAG_CHOICES, default='lead')
    owner = models.ForeignKey('User', blank=True, null=True, on_delete=models.SET_NULL, related_name='crm_contacts')
    notes = models.TextField(blank=True, default='')
    follow_up_date = models.DateField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'crm'

    def __str__(self):
        return self.name


class Sound(models.Model):
    """Uploaded audio, converted on save to 8kHz mono WAV (what Asterisk's native playback
    expects) and stored directly under /var/lib/asterisk/sounds/ (alongside Asterisk's own
    prompt files) so it's usable from dialplan apps (Playback, Background) by filename, no
    extension. Powers Settings -> Sounds; other features (Queue welcome sound, IVR) pick from
    this library. Music on Hold is a separate mechanism (see MusicOnHold) -- MOH tracks are
    plain files in a per-class folder, not Sound rows."""
    CATEGORY_CHOICES = [
        ('welcome', 'Welcome'),
        ('after_hours', 'After Hours'),
        ('other', 'Other'),
    ]

    name = models.CharField(max_length=100)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='other')
    filename = models.CharField(max_length=150, unique=True, help_text='Asterisk-ready filename, no extension, e.g. welcome-2026')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'sounds'

    def __str__(self):
        return self.name


class MusicOnHold(models.Model):
    """Asterisk realtime musiconhold class -- extconfig.conf maps the `musiconhold` family to
    this table (odbc,asterisk,musiconhold), one row per class, matched by name. Each class owns
    a dedicated folder under /var/lib/asterisk/moh/<name>/ (mode=files) -- Asterisk itself scans
    that directory and plays through whatever's in it, so individual tracks aren't tracked in
    the DB, just uploaded/deleted straight from the folder. Powers Settings -> Sounds."""
    name = models.CharField(max_length=80, primary_key=True)
    mode = models.CharField(max_length=80, default='files')
    directory = models.CharField(max_length=255, blank=True, null=True)
    # Always 'alpha' -- tracks are saved with a numeric filename prefix (01-, 02-, ...) that
    # encodes the admin's chosen play order, so alphabetical listing == that order. See
    # _next_moh_sequence()/reorder handling in core/views.py.
    sort = models.CharField(max_length=20, default='alpha')

    # Not part of Asterisk's musiconhold schema -- app-level only, same precedent as
    # Queue.description (Asterisk's realtime engine ignores columns it doesn't recognize).
    description = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        db_table = 'musiconhold'

    def __str__(self):
        return self.name


class CallTime(models.Model):
    """A named business-hours schedule (Settings -> Call Time), with an independent start/end
    time per day of week. Queues reference one via Queue.call_time to decide whether to play
    their Welcome or After Hours sound. App-level only -- not part of any Asterisk realtime
    schema. A blank pair for a given day means the queue is treated as after-hours all day."""
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100, unique=True)

    sat_start = models.TimeField(blank=True, null=True)
    sat_end = models.TimeField(blank=True, null=True)
    sun_start = models.TimeField(blank=True, null=True)
    sun_end = models.TimeField(blank=True, null=True)
    mon_start = models.TimeField(blank=True, null=True)
    mon_end = models.TimeField(blank=True, null=True)
    tue_start = models.TimeField(blank=True, null=True)
    tue_end = models.TimeField(blank=True, null=True)
    wed_start = models.TimeField(blank=True, null=True)
    wed_end = models.TimeField(blank=True, null=True)
    thu_start = models.TimeField(blank=True, null=True)
    thu_end = models.TimeField(blank=True, null=True)
    fri_start = models.TimeField(blank=True, null=True)
    fri_end = models.TimeField(blank=True, null=True)

    class Meta:
        db_table = 'call_time'
        verbose_name = 'Call Time'
        verbose_name_plural = 'Call Times'

    def __str__(self):
        return self.name


class Ivr(models.Model):
    """An interactive voice response menu (Settings -> IVR). Plays a greeting and waits for a
    single DTMF digit, then routes to an extension, a queue, or another IVR (a sub-menu) --
    driven by agi/router.py via native AGI GET DATA, not Asterisk dialplan. A Route with
    destination_type='ivr' points here by name. Key mappings are a free-form list (IvrOption
    below), not a fixed 0-9 grid -- add as many or as few as the menu needs. App-level only,
    no Asterisk realtime schema."""
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=80, unique=True)
    description = models.CharField(max_length=255, blank=True, default='')
    greeting_sound = models.ForeignKey(
        'Sound', blank=True, null=True, on_delete=models.SET_NULL, related_name='+',
        help_text='Prompt played when the menu starts',
    )
    invalid_sound = models.ForeignKey(
        'Sound', blank=True, null=True, on_delete=models.SET_NULL, related_name='+',
        help_text='Played before re-prompting on an invalid key or no input',
    )
    timeout = models.IntegerField(default=5, help_text='Seconds to wait for a key press')
    max_retries = models.IntegerField(default=3, help_text='Times to re-prompt before hanging up')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ivr'
        verbose_name = 'IVR'
        verbose_name_plural = 'IVRs'

    def __str__(self):
        return self.name


class IvrOption(models.Model):
    """One key mapping within an IVR menu -- press `digit`, route to `destination_type`
    `destination_id`. As many of these as needed can be attached to one Ivr."""
    DESTINATION_TYPE_CHOICES = [('extension', 'Extension'), ('queue', 'Group'), ('ivr', 'IVR')]
    DIGIT_CHOICES = [(d, d) for d in ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '*', '#']]

    ivr = models.ForeignKey('Ivr', on_delete=models.CASCADE, related_name='options')
    digit = models.CharField(max_length=1, choices=DIGIT_CHOICES)
    destination_type = models.CharField(max_length=20, choices=DESTINATION_TYPE_CHOICES)
    destination_id = models.CharField(max_length=80)

    class Meta:
        db_table = 'ivr_option'
        unique_together = [('ivr', 'digit')]
        ordering = ['digit']

    def __str__(self):
        return f'{self.ivr_id} — key {self.digit}'


class Blacklist(models.Model):
    """Settings -> Blacklist/DND. number is stored E.164-normalized (see
    core.views._normalize_phone) so lookups during call routing are a plain equality check
    regardless of how the caller ID or admin-entered number was originally formatted."""
    number = models.CharField(max_length=20, unique=True, db_index=True)
    reason = models.CharField(max_length=255, blank=True, default='')
    added_by = models.ForeignKey('User', on_delete=models.SET_NULL, null=True, blank=True, related_name='blacklist_entries')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'blacklist'
        verbose_name_plural = 'Blacklist'

    def __str__(self):
        return self.number


class LeadList(models.Model):
    """A named batch of lead numbers (e.g. "August Campaign") -- created first, then
    populated by bulk-uploading numbers into it, so Auto Dial / Broadcast campaigns pick a
    named list rather than one shared pool of numbers."""
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=255, blank=True, default='')
    created_by = models.ForeignKey('User', on_delete=models.SET_NULL, null=True, blank=True, related_name='lead_lists_created')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'lead_lists'
        verbose_name_plural = 'Lead Lists'
        ordering = ['-created_at']

    def __str__(self):
        return self.name


class Lead(models.Model):
    """A contact number belonging to a LeadList. Deliberately just a phone number -- no
    name/source/status fields -- since campaigns only ever need something to dial. number is
    stored digit-only normalized (see core.views._normalize_phone), same as Blacklist, so
    duplicates from repeated CSV imports into the same list collapse to one row."""
    lead_list = models.ForeignKey(LeadList, on_delete=models.CASCADE, related_name='leads')
    number = models.CharField(max_length=20, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'leads'
        verbose_name_plural = 'Leads'
        ordering = ['-created_at']
        unique_together = [('lead_list', 'number')]

    def __str__(self):
        return self.number


class Broadcast(models.Model):
    """A Voice Broadcast campaign -- dials every number in a LeadList (skipping Blacklist
    entries) and plays a recorded Sound, no agent involved (see ami/ami.py's broadcast dialer,
    which uses AMI Originate into agi/router.py's run_broadcast_message rather than bridging to
    a queue/agent). Optionally offers a "press N" menu while the message plays (see
    BroadcastDigitOption below). Reporting reuses Cdr instead of duplicating call data -- each
    Originate tags CDR(userfield) with this row's tag (see .tag), so the report page is just
    Cdr.objects.filter(userfield=broadcast.tag) and the general CDR/Reports page excludes
    that same tag prefix so broadcast traffic doesn't clutter regular call reporting."""
    STATUS_CHOICES = [
        ('draft', 'Draft'), ('running', 'Running'), ('paused', 'Paused'), ('completed', 'Completed'),
    ]

    name = models.CharField(max_length=100)
    lead_list = models.ForeignKey(LeadList, on_delete=models.PROTECT, related_name='broadcasts')
    sound = models.ForeignKey('Sound', on_delete=models.PROTECT, related_name='broadcasts')
    trunk = models.ForeignKey(
        'Endpoint', on_delete=models.PROTECT, related_name='broadcasts',
        limit_choices_to={'assigned_user__isnull': True},
    )
    concurrent_calls = models.PositiveSmallIntegerField(default=5)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')
    created_by = models.ForeignKey('User', on_delete=models.SET_NULL, null=True, blank=True, related_name='broadcasts_created')
    created_at = models.DateTimeField(auto_now_add=True)

    # Auto-starts this broadcast the first time ami.ami's periodic scheduled-broadcast check
    # sees status still 'draft' and scheduled_at in the past -- a manual Start button press
    # works exactly as before regardless of this field, so it's purely an optional convenience,
    # not a new status/state machine. scheduled_end_at is the mirror for stopping: a 'running'
    # broadcast past this time gets paused automatically, same as pressing Pause by hand (see
    # ami.ami._start_due_scheduled_broadcasts/_stop_due_scheduled_broadcasts).
    scheduled_at = models.DateTimeField(null=True, blank=True)
    scheduled_end_at = models.DateTimeField(null=True, blank=True)

    # "Delete" in the campaign list only sets this (never a real .delete()) -- the row, its
    # name, and every Cdr row already tagged with its .tag stay intact forever, so Reports ->
    # Broadcast can still show a deleted campaign's history by name. Only admin_broadcast_view's
    # own list query excludes is_deleted rows; nothing else needs to care.
    is_deleted = models.BooleanField(default=False)

    class Meta:
        db_table = 'broadcasts'
        verbose_name_plural = 'Broadcasts'
        ordering = ['-created_at']

    @property
    def tag(self):
        return f'Broadcast-{self.id}'

    def __str__(self):
        return self.name


class BroadcastDigitOption(models.Model):
    """One key mapping for a Broadcast's optional "press N" menu -- press `digit` while the
    message plays, route to `destination_type` `destination_id` (destination_id is an agent
    extension's PJSIP endpoint id for 'extension', a Queue name for 'queue', an Ivr name for
    'ivr', or optionally a Sound id for 'hangup'). As many of these as a campaign needs can be
    attached to one Broadcast; none at all means the message just plays through with no menu,
    same as before this existed (see agi/router.py's run_broadcast_message, and
    /etc/asterisk/extensions.conf's [broadcast] context, which now routes every broadcast call
    through that AGI script instead of a bare Playback). Mirrors IvrOption's shape but kept
    separate rather than reusing Ivr directly -- a broadcast message isn't a full IVR menu (no
    re-prompt/invalid-sound/retry loop)."""
    DESTINATION_TYPE_CHOICES = [('ivr', 'IVR'), ('queue', 'Group'), ('extension', 'User'), ('hangup', 'Hangup')]
    DIGIT_CHOICES = [(d, d) for d in ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '*', '#']]

    broadcast = models.ForeignKey(Broadcast, on_delete=models.CASCADE, related_name='digit_options')
    digit = models.CharField(max_length=1, choices=DIGIT_CHOICES)
    destination_type = models.CharField(max_length=20, choices=DESTINATION_TYPE_CHOICES)
    destination_id = models.CharField(max_length=80, blank=True, default='')

    class Meta:
        db_table = 'broadcast_digit_option'
        unique_together = [('broadcast', 'digit')]
        ordering = ['digit']

    def __str__(self):
        return f'{self.broadcast_id} — key {self.digit}'


class AutoDialCampaign(models.Model):
    """A Progressive Dialer campaign -- unlike Broadcast, every call goes to a live agent, one
    number at a time per agent, never more (see ami.ami's queue-member-status hook): the moment
    an agent in .queue_name goes Available, the system rings *that agent's own extension* with
    Context=from-internal, Exten=<next lead>, Priority=1 -- the exact same dialplan path a
    manual dial from their softphone takes. The agent's phone rings first, they answer it
    themselves, and only then does the lead get dialed -- so there's never a moment where audio
    just appears with no signal, and never more than one lead in flight per agent, so a lead
    picking up can never find nobody there (the abandoned-call problem predictive dialers have)."""
    STATUS_CHOICES = [
        ('draft', 'Draft'), ('running', 'Running'), ('paused', 'Paused'), ('completed', 'Completed'),
    ]

    name = models.CharField(max_length=100)
    lead_list = models.ForeignKey(LeadList, on_delete=models.PROTECT, related_name='auto_dial_campaigns')
    queue_name = models.CharField(max_length=128)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft')
    created_by = models.ForeignKey('User', on_delete=models.SET_NULL, null=True, blank=True, related_name='auto_dial_campaigns_created')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'auto_dial_campaigns'
        verbose_name_plural = 'Auto Dial Campaigns'
        ordering = ['-created_at']

    @property
    def tag(self):
        return f'AutoDial-{self.id}'

    def __str__(self):
        return self.name


class AutoDialAttempt(models.Model):
    """One lead dialed for one agent within an AutoDialCampaign -- lets the campaign know which
    leads are already spoken for (so a resume after pause doesn't redial them) and drives the
    Dialed/Connected stats on the Auto Dial page. status starts 'dialing' the instant the
    agent's extension is rung (see ami.ami._dial_next_auto_dial_lead) and is finalized off
    Asterisk's own DialStatus the moment the Dial() to this row's number resolves (see
    ami.ami._maybe_finish_auto_dial), or by a 60s self-heal timeout if that somehow never
    fires (see _auto_dial_attempt_timeout)."""
    STATUS_CHOICES = [
        ('dialing', 'Dialing'), ('connected', 'Connected'), ('no_answer', 'No Answer'),
        ('busy', 'Busy'), ('failed', 'Failed'),
    ]
    campaign = models.ForeignKey(AutoDialCampaign, on_delete=models.CASCADE, related_name='attempts')
    number = models.CharField(max_length=20)
    agent = models.ForeignKey('User', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='dialing')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'auto_dial_attempts'
        ordering = ['-created_at']
        # Two agents freeing up at the same instant can both read "not attempted yet" for the
        # same lead before either's row commits (see _dial_next_auto_dial_lead) -- this turns
        # that race into a clean IntegrityError there instead of silently double-dialing one
        # customer.
        constraints = [
            models.UniqueConstraint(fields=['campaign', 'number'], name='unique_auto_dial_attempt_per_lead'),
        ]


class DropCall(models.Model):
    """Marks a dropped (abandoned) queue call as handled once an agent calls the customer
    back -- the Drop Call panel excludes these, and the removal broadcasts live to every
    agent's dashboard (see ami/ami.py), not just the one who made the callback."""
    callid = models.CharField(max_length=32, unique=True, help_text="QueueLog's callid for the abandoned call")
    called_by = models.ForeignKey('User', on_delete=models.SET_NULL, null=True, related_name='drop_call_callbacks')
    called_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'drop_call'

    def __str__(self):
        return self.callid


class WhatsAppSettings(models.Model):
    """Settings -> WhatsApp. Always exactly one row (see load()) -- the WhatsApp Cloud API
    credentials needed to send messages and to verify/receive the inbound webhook. Unrelated
    to the SIP-based WhatsApp Business Calling trunk (Settings -> Registration) -- this is
    Meta's separate messaging product."""
    phone_number_id = models.CharField(max_length=40, blank=True, default='', help_text='From the WhatsApp Manager / Meta for Developers test number page')
    waba_id = models.CharField(max_length=40, blank=True, default='', verbose_name='WhatsApp Business Account ID', help_text="Needed only for the 'Re-subscribe App' button -- fixes the case where Meta is sending webhook events to a different app (e.g. its own default test-number app) instead of this one")
    access_token = models.CharField(max_length=1024, blank=True, default='')
    app_secret = models.CharField(max_length=255, blank=True, default='', help_text='Verifies the X-Hub-Signature-256 header on incoming webhook requests')
    verify_token = models.CharField(max_length=255, blank=True, default='', help_text='Any string you choose -- also entered in the Meta webhook setup form')

    ASSIGNMENT_MODE_CHOICES = [
        ('round_robin', 'Round Robin'),
        ('least_busy', 'Least Busy'),
        ('shared', 'All (Shared Inbox)'),
        ('manual', 'Manual (Admin Assigns)'),
    ]
    # How a brand-new WhatsApp conversation picks its agent -- see
    # core.views._assign_whatsapp_conversation. Note there's no separate "sticky" mode: number
    # is this model's own primary key, so a returning customer's messages always land back on
    # the same already-assigned row regardless of mode -- reassignment only ever happens once,
    # the very first time a number is ever seen.
    assignment_mode = models.CharField(max_length=20, choices=ASSIGNMENT_MODE_CHOICES, default='round_robin')

    class Meta:
        db_table = 'whatsapp_settings'
        verbose_name_plural = 'WhatsApp Settings'

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return 'WhatsApp Settings'


class WhatsAppConversation(models.Model):
    """One thread per customer phone number -- WhatsApp has no separate 'conversation id' of its
    own for messaging (unlike calling's per-call wacid), so the E.164 number itself is the
    natural key."""
    number = models.CharField(max_length=20, primary_key=True)
    contact_name = models.CharField(max_length=120, blank=True, default='')
    assigned_user = models.ForeignKey('User', null=True, blank=True, on_delete=models.SET_NULL, related_name='whatsapp_conversations')
    last_message_at = models.DateTimeField(null=True, blank=True)
    # WhatsApp only allows free-form replies within 24h of the customer's last message --
    # outside that window only a pre-approved template can be sent. Tracked here so the UI can
    # warn/disable the free-text box instead of the send silently failing at Meta's end.
    last_customer_message_at = models.DateTimeField(null=True, blank=True)
    # Bumped to now() whenever an admin opens this conversation -- messages.filter(direction='in',
    # created_at__gt=last_read_at) against this is how the sidebar's unread badge is computed.
    last_read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'whatsapp_conversations'
        ordering = ['-last_message_at']

    def __str__(self):
        return self.contact_name or self.number


class WhatsAppMessage(models.Model):
    DIRECTION_CHOICES = [('in', 'Inbound'), ('out', 'Outbound')]
    STATUS_CHOICES = [
        ('pending', 'Pending'), ('sent', 'Sent'), ('delivered', 'Delivered'),
        ('read', 'Read'), ('failed', 'Failed'), ('received', 'Received'),
    ]
    MEDIA_TYPE_CHOICES = [
        ('image', 'Image'), ('video', 'Video'), ('audio', 'Audio'), ('document', 'Document'),
    ]

    conversation = models.ForeignKey(WhatsAppConversation, on_delete=models.CASCADE, related_name='messages')
    direction = models.CharField(max_length=3, choices=DIRECTION_CHOICES)
    body = models.TextField(blank=True, default='')
    # A media message: the file itself lives on disk (see WHATSAPP_MEDIA_DIR in core/views.py,
    # same convention as call recordings living outside Django's own media framework) --
    # media_path is just the filename under that directory, served through
    # user_whatsapp_media_view rather than a public URL so only the conversation's own agent (or
    # an admin) can fetch it. body still carries the caption, if any, alongside the media.
    media_type = models.CharField(max_length=10, choices=MEDIA_TYPE_CHOICES, blank=True, default='')
    media_path = models.CharField(max_length=255, blank=True, default='')
    media_filename = models.CharField(max_length=255, blank=True, default='', help_text='Original filename (documents) or a display name')
    media_mime_type = models.CharField(max_length=100, blank=True, default='')
    wa_message_id = models.CharField(max_length=128, blank=True, default='', db_index=True, help_text="Meta's own message id -- used to match delivery/read status callbacks back to this row")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    sent_by = models.ForeignKey('User', null=True, blank=True, on_delete=models.SET_NULL, related_name='+', help_text='Agent who sent this (outbound only)')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'whatsapp_messages'
        ordering = ['created_at']

    def __str__(self):
        return f'{self.direction}: {self.body[:30]}'


class FacebookSettings(models.Model):
    """Settings -> Facebook. Always exactly one row (see load()) -- the Messenger Platform
    credentials needed to send messages and to verify/receive the inbound webhook, plus the
    same conversation-assignment setup as WhatsAppSettings (see core.views._assign_facebook_conversation,
    a straight copy of _assign_whatsapp_conversation with the model names swapped)."""
    page_id = models.CharField(max_length=40, blank=True, default='', help_text="Your Facebook Page's ID")
    page_access_token = models.CharField(max_length=1024, blank=True, default='')
    app_secret = models.CharField(max_length=255, blank=True, default='', help_text='Verifies the X-Hub-Signature-256 header on incoming webhook requests')
    verify_token = models.CharField(max_length=255, blank=True, default='', help_text='Any string you choose -- also entered in the Meta webhook setup form')

    ASSIGNMENT_MODE_CHOICES = [
        ('round_robin', 'Round Robin'),
        ('least_busy', 'Least Busy'),
        ('shared', 'All (Shared Inbox)'),
        ('manual', 'Manual (Admin Assigns)'),
    ]
    assignment_mode = models.CharField(max_length=20, choices=ASSIGNMENT_MODE_CHOICES, default='round_robin')

    class Meta:
        db_table = 'facebook_settings'
        verbose_name_plural = 'Facebook Settings'

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return 'Facebook Settings'


class FacebookConversation(models.Model):
    """One thread per Messenger PSID (page-scoped user id) -- same reasoning as
    WhatsAppConversation's number: Messenger has no separate 'conversation id' of its own, so
    the sender's PSID is the natural key."""
    psid = models.CharField(max_length=40, primary_key=True)
    contact_name = models.CharField(max_length=120, blank=True, default='')
    assigned_user = models.ForeignKey('User', null=True, blank=True, on_delete=models.SET_NULL, related_name='facebook_conversations')
    last_message_at = models.DateTimeField(null=True, blank=True)
    # Messenger only allows free-form replies within 24h of the customer's last message, same
    # rule as WhatsApp's messaging window -- tracked here for the same reason.
    last_customer_message_at = models.DateTimeField(null=True, blank=True)
    last_read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'facebook_conversations'
        ordering = ['-last_message_at']

    def __str__(self):
        return self.contact_name or self.psid


class FacebookMessage(models.Model):
    DIRECTION_CHOICES = [('in', 'Inbound'), ('out', 'Outbound')]
    STATUS_CHOICES = [
        ('pending', 'Pending'), ('sent', 'Sent'), ('delivered', 'Delivered'),
        ('read', 'Read'), ('failed', 'Failed'), ('received', 'Received'),
    ]
    # Messenger's own Send API attachment 'type' values -- 'file' is Messenger's name for what
    # WhatsAppMessage calls 'document', kept as-is here (rather than renamed for consistency)
    # since this value is sent to Meta verbatim as the attachment type.
    MEDIA_TYPE_CHOICES = [
        ('image', 'Image'), ('video', 'Video'), ('audio', 'Audio'), ('file', 'File'),
    ]

    conversation = models.ForeignKey(FacebookConversation, on_delete=models.CASCADE, related_name='messages')
    direction = models.CharField(max_length=3, choices=DIRECTION_CHOICES)
    body = models.TextField(blank=True, default='')
    media_type = models.CharField(max_length=10, choices=MEDIA_TYPE_CHOICES, blank=True, default='')
    media_path = models.CharField(max_length=255, blank=True, default='')
    media_filename = models.CharField(max_length=255, blank=True, default='', help_text='Original filename (documents) or a display name')
    media_mime_type = models.CharField(max_length=100, blank=True, default='')
    fb_message_id = models.CharField(max_length=128, blank=True, default='', db_index=True, help_text="Meta's own message id -- used to match delivery/read status callbacks back to this row")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    sent_by = models.ForeignKey('User', null=True, blank=True, on_delete=models.SET_NULL, related_name='+', help_text='Agent who sent this (outbound only)')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'facebook_messages'
        ordering = ['created_at']

    def __str__(self):
        return f'{self.direction}: {self.body[:30]}'
