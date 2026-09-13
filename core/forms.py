from django import forms

from core.models import ROUTE_SOURCE_ANY, Broadcast, BroadcastDigitOption, CallTime, Crm, Endpoint, Ivr, IvrOption, MusicOnHold, Queue, Route, Sound, User

PHONE_TYPE_CHOICES = [
    ('webrtc', 'WebRTC'),
    ('softphone', 'Softphone'),
]

# What each phone type needs on the PJSIP endpoint. A browser's RTCPeerConnection always
# offers DTLS-SRTP + ICE and never falls back to plain RTP, so WebRTC endpoints must carry
# these or calls fail negotiation outright ("Couldn't negotiate stream ... (nothing)").
# Softphones/desk phones use plain RTP and need none of this.
PHONE_TYPE_ENDPOINT_CONFIG = {
    'webrtc': dict(
        transport='transport-wss',
        allow='ulaw,alaw,opus',
        webrtc='yes',
        media_encryption='dtls',
        ice_support='yes',
        rtcp_mux='yes',
        use_avpf='yes',
        dtls_auto_generate_cert='yes',
    ),
    'softphone': dict(
        transport='transport-udp',
        allow='ulaw,alaw',
    ),
}

TEXT_CLASS = 'pl-9 block w-full px-3 py-1.5 border border-gray-300 rounded focus:outline-none focus:ring-0 focus:ring-indigo-500 focus:border-indigo-500 focus:bg-gray-100 text-xs placeholder:text-[11px]'
SELECT_CLASS = 'appearance-none pl-9 block w-full px-3 py-1.5 border border-gray-300 rounded focus:outline-none focus:ring-0 focus:ring-indigo-500 focus:border-indigo-500 focus:bg-gray-100 text-xs bg-white pr-8'

# Compact variants — Queue forms pack many fields per row (5 across), so these use tighter
# padding than TEXT_CLASS/SELECT_CLASS to fit without the row growing too tall.
COMPACT_TEXT_CLASS = 'pl-7 block w-full px-2 py-1 border border-gray-300 rounded focus:outline-none focus:ring-0 focus:ring-indigo-500 focus:border-indigo-500 focus:bg-gray-100 text-xs placeholder:text-[10px]'
COMPACT_SELECT_CLASS = 'appearance-none pl-7 block w-full px-2 py-1 border border-gray-300 rounded focus:outline-none focus:ring-0 focus:ring-indigo-500 focus:border-indigo-500 focus:bg-gray-100 text-xs bg-white pr-6'

# Same focus treatment as Settings -> Routing's inputs (border color + bg tint, no ring glow) —
# scoped to UserCreateForm only, TrunkCreateForm below keeps the original TEXT_CLASS/SELECT_CLASS.
USER_TEXT_CLASS = 'pl-9 block w-full px-3 py-1.5 border border-gray-300 rounded focus:outline-none focus:ring-0 focus:ring-indigo-500 focus:border-indigo-500 focus:bg-gray-100 text-xs placeholder:text-[11px]'
USER_SELECT_CLASS = 'appearance-none pl-9 block w-full px-3 py-1.5 border border-gray-300 rounded focus:outline-none focus:ring-0 focus:ring-indigo-500 focus:border-indigo-500 focus:bg-gray-100 text-xs bg-white pr-8'


class UserCreateForm(forms.Form):
    full_name = forms.CharField(max_length=255, label='Full Name', widget=forms.TextInput(attrs={'class': USER_TEXT_CLASS, 'placeholder': 'Full Name'}),)
    username = forms.CharField(max_length=150, label='Username', widget=forms.TextInput(attrs={'class': USER_TEXT_CLASS, 'placeholder': 'username'}), )
    email = forms.EmailField(required=False, label='Email', widget=forms.EmailInput(attrs={'class': USER_TEXT_CLASS, 'placeholder': 'user@example.com'}),)
    password = forms.CharField(min_length=6, label='Password', widget=forms.PasswordInput(attrs={'class': USER_TEXT_CLASS + ' pr-10', 'placeholder': '••••••••', 'autocomplete': 'new-password'}),)
    phone_type = forms.ChoiceField(choices=PHONE_TYPE_CHOICES, label='Phone Type',widget=forms.Select(attrs={'class': USER_SELECT_CLASS}),)
    role = forms.ChoiceField(choices=User.ROLE_CHOICES, label='Role',widget=forms.Select(attrs={'class': USER_SELECT_CLASS}),)

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError('This username is already taken.')
        return username


REGISTRATION_TYPE_CHOICES = [
    ('userpass', 'Username & Password'),
    ('ip', 'IP Authentication'),
    ('whatsapp', 'WhatsApp Business Calling'),
]


class TrunkCreateForm(forms.Form):
    name = forms.CharField(max_length=40, label='Trunk Name', widget=forms.TextInput(attrs={'class': TEXT_CLASS, 'placeholder': 'banglalink-main'}))
    registration_type = forms.ChoiceField(choices=REGISTRATION_TYPE_CHOICES, label='Registration Type', widget=forms.Select(attrs={'class': SELECT_CLASS}))
    host = forms.CharField(max_length=255, required=False, label='Host / IP', widget=forms.TextInput(attrs={'class': TEXT_CLASS, 'placeholder': 'sip.provider.com'}))
    port = forms.IntegerField(required=False, initial=5060, label='Port', widget=forms.NumberInput(attrs={'class': TEXT_CLASS, 'placeholder': '5060'}))
    username = forms.CharField(max_length=80, required=False, label='Username', widget=forms.TextInput(attrs={'class': TEXT_CLASS, 'placeholder': 'account_id'}))
    password = forms.CharField(max_length=80, required=False, label='Password', widget=forms.PasswordInput(attrs={'class': TEXT_CLASS, 'placeholder': '••••••••', 'autocomplete': 'new-password'}))
    ip_addresses = forms.CharField(required=False, label='Allowed IP / CIDR (comma separated)', widget=forms.TextInput(attrs={'class': TEXT_CLASS, 'placeholder': '203.0.113.10, 203.0.113.0/24'}))

    # WhatsApp Business Calling -- just the business phone number + the sip_user_password from
    # Meta's GET /<PHONE_NUMBER_ID>/settings?include_sip_credentials=true. Where inbound calls
    # ring is set up on Settings -> Routing afterwards, same as any other trunk.
    whatsapp_number = forms.CharField(max_length=20, required=False, label='WhatsApp Business Number', widget=forms.TextInput(attrs={'class': TEXT_CLASS, 'placeholder': '+15552014038'}))
    whatsapp_sip_password = forms.CharField(max_length=80, required=False, label='SIP Password', widget=forms.PasswordInput(attrs={'class': TEXT_CLASS, 'placeholder': '••••••••', 'autocomplete': 'new-password'}))

    def clean_name(self):
        name = self.cleaned_data['name'].strip().lower().replace(' ', '-')
        if Endpoint.objects.filter(id=name).exists():
            raise forms.ValidationError('A trunk or extension with this name already exists.')
        return name

    def clean_whatsapp_sip_password(self):
        # This gets copy-pasted straight out of Meta's GET .../settings JSON response, where
        # it's quoted ("sip_user_password": "..."), so a stray leading/trailing " from
        # over-selecting the value is an easy mistake -- silently breaks every digest auth
        # attempt (wrong password) rather than something a form validator would ever catch,
        # so just strip it instead of letting it corrupt the trunk.
        return self.cleaned_data['whatsapp_sip_password'].strip().strip('"')

    def clean(self):
        cleaned = super().clean()
        reg_type = cleaned.get('registration_type')
        name = cleaned.get('name')
        username = cleaned.get('username')
        if reg_type == 'userpass':
            if not cleaned.get('host', '').strip():
                self.add_error('host', 'Host / IP is required.')
            if not username:
                self.add_error('username', 'Username is required for username/password registration.')
            elif name and username.strip().lower() == name.strip().lower():
                self.add_error('username', 'Username cannot be the same as the Trunk Name.')
            if not cleaned.get('password'):
                self.add_error('password', 'Password is required for username/password registration.')
        elif reg_type == 'ip':
            if not cleaned.get('host', '').strip():
                self.add_error('host', 'Host / IP is required.')
            if not cleaned.get('ip_addresses', '').strip():
                self.add_error('ip_addresses', 'At least one IP or CIDR is required for IP authentication.')
        elif reg_type == 'whatsapp':
            number = cleaned.get('whatsapp_number', '').strip()
            if not number:
                self.add_error('whatsapp_number', 'WhatsApp business number is required.')
            if not cleaned.get('whatsapp_sip_password', '').strip():
                self.add_error('whatsapp_sip_password', 'SIP password is required.')
        return cleaned


class QueueForm(forms.ModelForm):
    # Overrides the model's plain CharField -- picks from existing MusicOnHold classes
    # (Settings -> Sounds manages the classes and their tracks) rather than free text.
    musicclass = forms.ChoiceField(required=False, widget=forms.Select(attrs={'class': COMPACT_SELECT_CLASS}))

    class Meta:
        model = Queue
        exclude = ['is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': COMPACT_TEXT_CLASS, 'placeholder': 'sales-queue'}),
            'description': forms.TextInput(attrs={'class': COMPACT_TEXT_CLASS, 'placeholder': 'Outbound-focused sales agents'}),
            'strategy': forms.Select(attrs={'class': COMPACT_SELECT_CLASS}),
            'timeout': forms.NumberInput(attrs={'class': COMPACT_TEXT_CLASS}),
            'retry': forms.NumberInput(attrs={'class': COMPACT_TEXT_CLASS}),
            'wrapuptime': forms.NumberInput(attrs={'class': COMPACT_TEXT_CLASS}),
            'maxlen': forms.NumberInput(attrs={'class': COMPACT_TEXT_CLASS}),
            'servicelevel': forms.NumberInput(attrs={'class': COMPACT_TEXT_CLASS}),
            'weight': forms.NumberInput(attrs={'class': COMPACT_TEXT_CLASS}),
            'ringinuse': forms.Select(attrs={'class': COMPACT_SELECT_CLASS}),
            'autofill': forms.Select(attrs={'class': COMPACT_SELECT_CLASS}),
            'autopause': forms.Select(attrs={'class': COMPACT_SELECT_CLASS}),
            'reportholdtime': forms.Select(attrs={'class': COMPACT_SELECT_CLASS}),
            'timeoutrestart': forms.Select(attrs={'class': COMPACT_SELECT_CLASS}),
            'joinempty': forms.TextInput(attrs={'class': COMPACT_TEXT_CLASS, 'placeholder': 'paused,invalid'}),
            'leavewhenempty': forms.TextInput(attrs={'class': COMPACT_TEXT_CLASS, 'placeholder': 'paused,invalid'}),
            'context': forms.TextInput(attrs={'class': COMPACT_TEXT_CLASS, 'placeholder': 'from-internal'}),
            'announce': forms.TextInput(attrs={'class': COMPACT_TEXT_CLASS, 'placeholder': 'queue-announce'}),
            'periodic_announce': forms.TextInput(attrs={'class': COMPACT_TEXT_CLASS}),
            'periodic_announce_frequency': forms.NumberInput(attrs={'class': COMPACT_TEXT_CLASS, 'placeholder': '30'}),
            'announce_frequency': forms.NumberInput(attrs={'class': COMPACT_TEXT_CLASS, 'placeholder': '60'}),
            'announce_holdtime': forms.Select(attrs={'class': COMPACT_SELECT_CLASS}),
            'announce_position': forms.Select(attrs={'class': COMPACT_SELECT_CLASS}),
            'monitor_format': forms.TextInput(attrs={'class': COMPACT_TEXT_CLASS, 'placeholder': 'wav'}),
            'membermacro': forms.TextInput(attrs={'class': COMPACT_TEXT_CLASS}),
            'membergosub': forms.TextInput(attrs={'class': COMPACT_TEXT_CLASS}),
            'setinterfacevar': forms.Select(attrs={'class': COMPACT_SELECT_CLASS}),
            'setqueuevar': forms.Select(attrs={'class': COMPACT_SELECT_CLASS}),
            'setqueueentryvar': forms.Select(attrs={'class': COMPACT_SELECT_CLASS}),
            'eventmemberstatus': forms.Select(attrs={'class': COMPACT_SELECT_CLASS}),
            'eventwhencalled': forms.Select(attrs={'class': COMPACT_SELECT_CLASS}),
            'memberdelay': forms.NumberInput(attrs={'class': COMPACT_TEXT_CLASS}),
            'welcome_sound': forms.Select(attrs={'class': COMPACT_SELECT_CLASS}),
            'after_hours_sound': forms.Select(attrs={'class': COMPACT_SELECT_CLASS}),
            'call_time': forms.Select(attrs={'class': COMPACT_SELECT_CLASS}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.required = False
        self.fields['name'].required = True
        self.fields['strategy'].required = True
        self.fields['musicclass'].choices = [('', 'None (no hold music)')] + [
            (name, name) for name in MusicOnHold.objects.order_by('name').values_list('name', flat=True)
        ]
        self.fields['welcome_sound'].queryset = Sound.objects.filter(category='welcome').order_by('name')
        self.fields['after_hours_sound'].queryset = Sound.objects.filter(category='after_hours').order_by('name')
        self.fields['call_time'].queryset = CallTime.objects.order_by('name')
        self.fields['welcome_sound'].empty_label = 'None'
        self.fields['after_hours_sound'].empty_label = 'None'
        self.fields['call_time'].empty_label = 'None (welcome always plays)'

    def clean_name(self):
        name = self.cleaned_data['name'].strip().lower().replace(' ', '-')
        qs = Queue.objects.filter(name=name)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError('A group with this name already exists.')
        return name


class QueueQuickCreateForm(forms.ModelForm):
    """Just enough to create a queue — the rest (strategy, timeouts, announcements, ...) uses
    model defaults and gets tuned later on the queue's own settings page."""
    class Meta:
        model = Queue
        fields = ['name', 'description', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': TEXT_CLASS, 'placeholder': 'Sales Team'}),
            'description': forms.TextInput(attrs={'class': TEXT_CLASS, 'placeholder': 'Outbound-focused sales agents'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-checkbox h-4 w-4 text-indigo-600 rounded border-slate-300 focus:ring-indigo-500'}),
        }

    def clean_name(self):
        name = self.cleaned_data['name'].strip().lower().replace(' ', '-')
        if Queue.objects.filter(name=name).exists():
            raise forms.ValidationError('A queue with this name already exists.')
        return name


class AgentMultipleChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        name = obj.assigned_user.full_name if obj.assigned_user else None
        return f'{obj.id} — {name}' if name else obj.id


class QueueMembersForm(forms.Form):
    """Just validates which extensions are legitimate agent choices — each one's penalty is
    a separate per-row field (penalty_<extension_id>) parsed directly from POST in the view,
    since every agent needs their own value, not one shared default."""
    members = AgentMultipleChoiceField(
        queryset=Endpoint.objects.filter(assigned_user__role='user').order_by('id'),
        required=False, label='Agents',
        widget=forms.CheckboxSelectMultiple(attrs={'class': 'w-4 h-4 border border-gray-300 rounded text-indigo-600 focus:ring-2 focus:ring-indigo-400'}),
    )


class RouteForm(forms.ModelForm):
    """Create-only form backing Settings → Routing (Call Routes). One trunk carries at most one
    inbound route and one outbound route -- matches the trunk-picker's client-side filtering."""
    class Meta:
        model = Route
        fields = ['name', 'direction', 'trunk', 'did', 'destination_type', 'destination_id', 'source_type', 'source_id', 'is_active']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['trunk'].queryset = Endpoint.objects.filter(assigned_user__isnull=True).order_by('id')
        for name in ('did', 'destination_type', 'destination_id', 'source_type', 'source_id'):
            self.fields[name].required = False

    def clean_name(self):
        name = self.cleaned_data['name'].strip().lower().replace(' ', '-')
        if Route.objects.filter(name=name).exists():
            raise forms.ValidationError('A route with this name already exists.')
        return name

    def clean(self):
        cleaned_data = super().clean()
        direction = cleaned_data.get('direction')
        trunk = cleaned_data.get('trunk')
        if not direction or not trunk:
            return cleaned_data

        if Route.objects.filter(trunk=trunk, direction=direction).exists():
            raise forms.ValidationError(f'Trunk "{trunk.id}" already has a{"n" if direction == "inbound" else ""} {direction} route.')

        if direction == 'inbound':
            did = (cleaned_data.get('did') or '').strip()
            dest_type = cleaned_data.get('destination_type')
            dest_id = (cleaned_data.get('destination_id') or '').strip()
            if not did:
                self.add_error('did', 'DID number is required for an inbound route.')
            elif Route.objects.filter(direction='inbound', did=did).exists():
                self.add_error('did', f'DID "{did}" is already routed.')
            if not dest_type:
                self.add_error('destination_type', 'Destination type is required for an inbound route.')
            if not dest_id:
                self.add_error('destination_id', 'Destination is required for an inbound route.')
            elif dest_type == 'extension' and not Endpoint.objects.filter(id=dest_id, assigned_user__role='user').exists():
                self.add_error('destination_id', f'Extension "{dest_id}" not found.')
            elif dest_type == 'queue' and not Queue.objects.filter(name=dest_id).exists():
                self.add_error('destination_id', f'Group "{dest_id}" not found.')
            elif dest_type == 'ivr' and not Ivr.objects.filter(name=dest_id).exists():
                self.add_error('destination_id', f'IVR "{dest_id}" not found.')
            cleaned_data['source_type'] = ''
            cleaned_data['source_id'] = ''
        else:
            source_type = cleaned_data.get('source_type')
            source_id = (cleaned_data.get('source_id') or '').strip()
            if source_id:
                if source_type == 'extension' and not Endpoint.objects.filter(id=source_id, assigned_user__role='user').exists():
                    self.add_error('source_id', f'Extension "{source_id}" not found.')
                elif source_type == 'queue' and not Queue.objects.filter(name=source_id).exists():
                    self.add_error('source_id', f'Group "{source_id}" not found.')
                if Route.objects.filter(direction='outbound', source_type=source_type, source_id=source_id).exists():
                    self.add_error('source_id', f'"{source_id}" already has an outbound route.')
            else:
                # Blank source = catch-all fallback; only one may exist at a time, and it's
                # stored with a sentinel (ROUTE_SOURCE_ANY) since REALTIME_FIELD's single-field
                # match can't otherwise tell an outbound catch-all row apart from an inbound
                # route's blank source_id.
                if Route.objects.filter(direction='outbound', source_id=ROUTE_SOURCE_ANY).exists():
                    raise forms.ValidationError('A catch-all outbound route (no source selected) already exists.')
                source_type = ''
                source_id = ROUTE_SOURCE_ANY
            cleaned_data['source_type'] = source_type
            cleaned_data['source_id'] = source_id
            cleaned_data['did'] = ''
            cleaned_data['destination_type'] = ''
            cleaned_data['destination_id'] = ''
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.source_type = self.cleaned_data.get('source_type', '')
        instance.source_id = self.cleaned_data.get('source_id', '')
        instance.did = self.cleaned_data.get('did', '')
        instance.destination_type = self.cleaned_data.get('destination_type', '')
        instance.destination_id = self.cleaned_data.get('destination_id', '')
        if commit:
            instance.save()
        return instance


CALL_TIME_DAYS = ['sat', 'sun', 'mon', 'tue', 'wed', 'thu', 'fri']
# Same focus treatment as TEXT_CLASS/COMPACT_TEXT_CLASS (Users/Registration inputs), sized to
# sit in a dense per-day table cell instead of a full-width labeled field.
CALL_TIME_TIME_CLASS = 'ct-time w-full px-2 py-1 border border-gray-300 rounded focus:outline-none focus:ring-0 focus:ring-indigo-500 focus:border-indigo-500 focus:bg-gray-100 text-xs'


class CallTimeForm(forms.ModelForm):
    class Meta:
        model = CallTime
        fields = ['name'] + [f'{day}_{edge}' for day in CALL_TIME_DAYS for edge in ('start', 'end')]
        widgets = {
            'name': forms.TextInput(attrs={'class': TEXT_CLASS, 'placeholder': 'e.g. Default Hours'}),
            **{
                f'{day}_{edge}': forms.TimeInput(attrs={'type': 'time', 'class': CALL_TIME_TIME_CLASS})
                for day in CALL_TIME_DAYS for edge in ('start', 'end')
            },
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.required = False
        self.fields['name'].required = True

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        qs = CallTime.objects.filter(name__iexact=name)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError('A schedule with this name already exists.')
        return name

    def clean(self):
        cleaned = super().clean()
        for day in CALL_TIME_DAYS:
            start = cleaned.get(f'{day}_start')
            end = cleaned.get(f'{day}_end')
            if bool(start) != bool(end):
                self.add_error(f'{day}_end' if start else f'{day}_start', 'Set both start and end, or leave both blank.')
        return cleaned


# Key-mapping row selects (digit / destination type) sit inline with no icon prefix -- same
# focus treatment as Call Time's day-table inputs, just not the icon-padded TEXT_CLASS/
# SELECT_CLASS used for the labeled fields above them (name/description/sounds/timeout).
IVR_ROW_SELECT_CLASS = 'border border-gray-300 rounded focus:outline-none focus:ring-0 focus:ring-indigo-500 focus:border-indigo-500 focus:bg-gray-100 text-xs bg-white px-2 py-1.5'


class IvrForm(forms.ModelForm):
    class Meta:
        model = Ivr
        fields = ['name', 'description', 'greeting_sound', 'invalid_sound', 'timeout', 'max_retries', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': TEXT_CLASS, 'placeholder': 'Main Menu'}),
            'description': forms.TextInput(attrs={'class': TEXT_CLASS, 'placeholder': 'Top-level inbound menu'}),
            'greeting_sound': forms.Select(attrs={'class': SELECT_CLASS}),
            'invalid_sound': forms.Select(attrs={'class': SELECT_CLASS}),
            'timeout': forms.NumberInput(attrs={'class': TEXT_CLASS}),
            'max_retries': forms.NumberInput(attrs={'class': TEXT_CLASS}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.required = False
        self.fields['name'].required = True
        self.fields['greeting_sound'].queryset = Sound.objects.order_by('name')
        self.fields['invalid_sound'].queryset = Sound.objects.order_by('name')
        self.fields['greeting_sound'].empty_label = 'None'
        self.fields['invalid_sound'].empty_label = 'None'

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        qs = Ivr.objects.filter(name__iexact=name)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError('An IVR with this name already exists.')
        return name


class IvrOptionForm(forms.ModelForm):
    """One row in the IVR key-mapping list. `ivr_name` (passed via the formset's form_kwargs)
    is only used to reject a menu routing into itself; it isn't a model field."""
    class Meta:
        model = IvrOption
        fields = ['digit', 'destination_type', 'destination_id']
        widgets = {
            'digit': forms.Select(attrs={'class': IVR_ROW_SELECT_CLASS + ' ivr-opt-digit', 'style': 'width:70px'}),
            'destination_type': forms.Select(attrs={'class': IVR_ROW_SELECT_CLASS + ' ivr-opt-type flex-1'}),
            'destination_id': forms.HiddenInput(attrs={'class': 'ivr-opt-dest-id'}),
        }

    def __init__(self, *args, ivr_name=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.ivr_name = ivr_name
        # Blank leading choices so a browser's default "first option selected" rendering means
        # an untouched extra row submits '' (genuinely unfilled), not a misleading real value --
        # otherwise a never-touched blank row would look "filled" and fail validation below.
        self.fields['digit'].choices = [('', '—')] + list(IvrOption.DIGIT_CHOICES)
        self.fields['digit'].required = False
        self.fields['destination_type'].choices = [('', 'Select type…')] + list(IvrOption.DESTINATION_TYPE_CHOICES)
        self.fields['destination_type'].required = False
        self.fields['destination_id'].required = False

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('DELETE'):
            return cleaned
        digit = cleaned.get('digit')
        dtype = cleaned.get('destination_type')
        did = (cleaned.get('destination_id') or '').strip()
        if not digit and not dtype and not did:
            return cleaned  # untouched extra row -- ignore
        if not digit:
            raise forms.ValidationError('Choose a key.')
        if not dtype or not did:
            raise forms.ValidationError('Choose both a destination type and a destination.')
        if dtype == 'extension' and not Endpoint.objects.filter(id=did, assigned_user__role='user').exists():
            raise forms.ValidationError(f'Extension "{did}" not found.')
        elif dtype == 'queue' and not Queue.objects.filter(name=did).exists():
            raise forms.ValidationError(f'Group "{did}" not found.')
        elif dtype == 'ivr':
            if self.ivr_name and did == self.ivr_name:
                raise forms.ValidationError('An IVR cannot route to itself.')
            elif not Ivr.objects.filter(name=did).exists():
                raise forms.ValidationError(f'IVR "{did}" not found.')
        return cleaned


IvrOptionFormSet = forms.inlineformset_factory(
    Ivr, IvrOption, form=IvrOptionForm, extra=1, can_delete=True,
)


class BroadcastDigitOptionForm(forms.ModelForm):
    """One row in a Broadcast's optional "press N" key-mapping list -- same shape as
    IvrOptionForm, but 'hangup' is a real destination (destination_id optionally names a Sound
    to play -- e.g. a "thanks for calling" message -- before hanging up; blank just hangs up
    immediately, same as before this existed) and there's no self-reference check (a Broadcast
    isn't an Ivr)."""
    class Meta:
        model = BroadcastDigitOption
        fields = ['digit', 'destination_type', 'destination_id']
        widgets = {
            'digit': forms.Select(attrs={'class': IVR_ROW_SELECT_CLASS + ' broadcast-opt-digit', 'style': 'width:70px'}),
            'destination_type': forms.Select(attrs={'class': IVR_ROW_SELECT_CLASS + ' broadcast-opt-type flex-1'}),
            'destination_id': forms.HiddenInput(attrs={'class': 'broadcast-opt-dest-id'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Blank leading choices so a browser's default "first option selected" rendering means
        # an untouched extra row submits '' (genuinely unfilled), not a misleading real value --
        # otherwise a never-touched blank row would look "filled" and fail validation below.
        self.fields['digit'].choices = [('', '—')] + list(BroadcastDigitOption.DIGIT_CHOICES)
        self.fields['digit'].required = False
        self.fields['destination_type'].choices = [('', 'Select type…')] + list(BroadcastDigitOption.DESTINATION_TYPE_CHOICES)
        self.fields['destination_type'].required = False
        self.fields['destination_id'].required = False

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('DELETE'):
            return cleaned
        digit = cleaned.get('digit')
        dtype = cleaned.get('destination_type')
        did = (cleaned.get('destination_id') or '').strip()
        if not digit and not dtype and not did:
            return cleaned  # untouched extra row -- ignore
        if not digit:
            raise forms.ValidationError('Choose a key.')
        if not dtype:
            raise forms.ValidationError('Choose a destination type.')
        if dtype == 'hangup':
            # Optional -- did names a Sound to play before hanging up ("thanks for calling");
            # blank is a perfectly valid immediate hangup, unlike queue/ivr below.
            if did and not Sound.objects.filter(id=did).exists():
                raise forms.ValidationError(f'Sound "{did}" not found.')
        elif dtype == 'queue':
            if not did:
                raise forms.ValidationError('Choose a destination.')
            if not Queue.objects.filter(name=did).exists():
                raise forms.ValidationError(f'Group "{did}" not found.')
        elif dtype == 'ivr':
            if not did:
                raise forms.ValidationError('Choose a destination.')
            if not Ivr.objects.filter(name=did).exists():
                raise forms.ValidationError(f'IVR "{did}" not found.')
        elif dtype == 'extension':
            if not did:
                raise forms.ValidationError('Choose a destination.')
            if not Endpoint.objects.filter(id=did, assigned_user__role='user').exists():
                raise forms.ValidationError(f'User "{did}" not found.')
        return cleaned


BroadcastDigitOptionFormSet = forms.inlineformset_factory(
    Broadcast, BroadcastDigitOption, form=BroadcastDigitOptionForm, extra=1, can_delete=True,
)


class CrmForm(forms.ModelForm):
    class Meta:
        model = Crm
        fields = ['name', 'phone', 'email', 'tag', 'owner', 'follow_up_date', 'notes']
        widgets = {
            'name': forms.TextInput(attrs={'class': TEXT_CLASS, 'placeholder': 'Abdul Karim'}),
            'phone': forms.TextInput(attrs={'class': TEXT_CLASS, 'placeholder': '+8801712345678'}),
            'email': forms.EmailInput(attrs={'class': TEXT_CLASS, 'placeholder': 'name@example.com'}),
            'tag': forms.Select(attrs={'class': SELECT_CLASS}),
            'owner': forms.Select(attrs={'class': SELECT_CLASS}),
            'follow_up_date': forms.DateInput(attrs={'class': TEXT_CLASS, 'type': 'date'}),
            'notes': forms.Textarea(attrs={
                'class': 'block w-full px-3 py-1.5 border border-gray-300 rounded focus:outline-none focus:ring-0 focus:ring-indigo-500 focus:border-indigo-500 focus:bg-gray-100 text-xs placeholder:text-[11px]',
                'rows': 2, 'placeholder': 'Notes...',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.required = False
        self.fields['name'].required = True
        self.fields['phone'].required = True
        self.fields['owner'].queryset = User.objects.order_by('full_name')
        self.fields['owner'].empty_label = 'Unassigned'

    def clean_phone(self):
        phone = self.cleaned_data['phone'].strip()
        qs = Crm.objects.filter(phone=phone)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError('A contact with this phone number already exists.')
        return phone
