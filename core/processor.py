from core.models import Auth

# Fixed app-wide display format (DD-MM-YYYY, 12-hour) -- used wherever a template would
# otherwise hardcode a |date filter string, so it's consistent everywhere at once.
DATE_FORMAT = 'd-m-Y'
TIME_FORMAT = 'h:i:s A'


def general_settings(request):
    return {
        'DATE_FORMAT': DATE_FORMAT,
        'TIME_FORMAT': TIME_FORMAT,
        'DATETIME_FORMAT': f'{DATE_FORMAT} {TIME_FORMAT}',
    }


def webrtc_credentials(request):
    """Makes the logged-in agent's SIP credentials available to every user-panel
    template, since the WebRTC widget is now embedded in user/base.html (shared
    across all user pages) instead of living in its own popup page. Also powers the
    admin Monitoring page's Spy/Whisper/Barge dialer (dials the *55/*56/*57 feature
    codes directly from the admin's own WebRTC session).

    Only returned for endpoints actually provisioned as phone_type='webrtc' (see
    PHONE_TYPE_ENDPOINT_CONFIG in core/forms.py) -- a 'softphone' endpoint has no DTLS/ICE
    config, so letting the widget register it over WSS anyway doesn't give the agent a working
    second device, it just fights their real softphone for the AOR's one contact slot (see
    _provision_extension's max_contacts=1) and leaves both broken."""
    if not (request.path.startswith('/user/') or request.path == '/admin/monitoring/'):
        return {}
    if not request.user.is_authenticated:
        return {}
    endpoint = request.user.pjsip_endpoints.first()
    if not endpoint or endpoint.webrtc != 'yes':
        return {}
    auth = Auth.objects.filter(id=endpoint.auth).first()
    from ami.ami import get_current_statuses
    return {
        'SERVER_IP': 's10.ihelpbd.com',
        'extension': endpoint.id,
        'extension_password': auth.password if auth else '',
        'is_paused': get_current_statuses().get(endpoint.id) == 'Paused',
    }
