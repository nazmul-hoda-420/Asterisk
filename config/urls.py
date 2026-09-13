
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import path
from core import views

urlpatterns = [
    # path('admin/', admin.site.urls),
    path('', views.login_view, name='login'),
    path('privacy-policy/', views.privacy_policy_view, name='privacy_policy'),
    path('logout/', views.logout_view, name='logout'),

    path('user/', views.user_dashboard_view, name='user_dashboard'),
    path('user/conversation-thread/', views.user_conversation_thread_view, name='user_conversation_thread'),
    path('user/whatsapp/media/<int:message_id>/', views.user_whatsapp_media_view, name='user_whatsapp_media'),
    path('user/facebook/media/<int:message_id>/', views.user_facebook_media_view, name='user_facebook_media'),
    path('user/drop-call/', views.user_mark_dropcall_view, name='drop_call'),
    path('user/toggle-pause/', views.user_toggle_pause_view, name='toggle_pause'),
    path('user/transfer/', views.user_transfer_view, name='user_transfer'),

    path('admin/', views.admin_view, name='admin'),
    path('admin/agent-performance/', views.admin_agent_performance_view, name='admin_agent_performance'),
    path('admin/activity/', views.admin_activity_view, name='admin_activity'),
    path('admin/cdr/', views.admin_cdr_view, name='admin_cdr'),
    path('admin/cdr/recording/<str:uniqueid>/', views.admin_cdr_recording_view, name='admin_cdr_recording'),
    path('admin/crm/', views.admin_crm_view, name='admin_crm'),
    path('admin/cel/', views.admin_cel_view, name='admin_cel'),
    path('admin/queue-log/', views.admin_queue_log_view, name='admin_queue_log'),
    path('admin/settings/', views.admin_settings_view, name='admin_settings'),
    path('admin/settings/users/', views.admin_settings_users_view, name='admin_settings_users'),
    path('admin/settings/users/<int:user_id>/delete/', views.admin_settings_users_delete_view, name='admin_settings_users_delete'),
    path('admin/settings/users/<int:user_id>/toggle/', views.admin_settings_users_toggle_view, name='admin_settings_users_toggle'),
    path('admin/settings/registration/', views.admin_settings_registration_view, name='admin_settings_registration'),
    path('admin/settings/registration/<str:trunk_id>/delete/', views.admin_settings_registration_delete_view, name='admin_settings_registration_delete'),
    path('admin/settings/registration/<str:trunk_id>/toggle/', views.admin_settings_registration_toggle_view, name='admin_settings_registration_toggle'),
    path('admin/settings/call-time/', views.admin_settings_call_time_view, name='admin_settings_call_time'),
    path('admin/auto-dial/', views.admin_auto_dial_view, name='admin_auto_dial'),
    path('admin/auto-dial/<int:campaign_id>/start/', views.admin_auto_dial_start_view, name='admin_auto_dial_start'),
    path('admin/auto-dial/<int:campaign_id>/pause/', views.admin_auto_dial_pause_view, name='admin_auto_dial_pause'),
    path('admin/broadcast/', views.admin_broadcast_view, name='admin_broadcast'),
    path('admin/broadcast/new/', views.admin_broadcast_form_view, name='admin_broadcast_new'),
    path('admin/broadcast/<int:broadcast_id>/edit/', views.admin_broadcast_form_view, name='admin_broadcast_edit'),
    path('admin/broadcast/<int:broadcast_id>/', views.admin_broadcast_detail_view, name='admin_broadcast_detail'),
    path('admin/reports/broadcast/', views.admin_broadcast_report_view, name='admin_broadcast_report'),
    path('admin/broadcast/<int:broadcast_id>/start/', views.admin_broadcast_start_view, name='admin_broadcast_start'),
    path('admin/broadcast/<int:broadcast_id>/pause/', views.admin_broadcast_pause_view, name='admin_broadcast_pause'),
    path('admin/settings/ivr/', views.admin_settings_ivr_view, name='admin_settings_ivr'),
    
    path('admin/settings/queue/', views.admin_settings_queue_view, name='admin_settings_queue'),
    path('admin/settings/queue/<str:queue_name>/edit/', views.admin_settings_queue_form_view, name='admin_settings_queue_edit'),
    path('admin/settings/queue/<str:queue_name>/delete/', views.admin_settings_queue_delete_view, name='admin_settings_queue_delete'),
    path('admin/settings/queue/<str:queue_name>/toggle/', views.admin_settings_queue_toggle_view, name='admin_settings_queue_toggle'),
    path('admin/settings/queue/<str:queue_name>/members/', views.admin_settings_queue_members_update_view, name='admin_settings_queue_members_update'),
    path('admin/settings/routing/', views.admin_settings_routing_view, name='admin_settings_routing'),
    path('admin/settings/sounds/', views.admin_settings_sounds_view, name='admin_settings_sounds'),
    path('admin/settings/sounds/moh/<str:class_name>/<path:filename>/preview/', views.admin_settings_moh_track_preview_view, name='admin_settings_moh_track_preview'),
    path('admin/settings/sounds/<path:filename>/preview/', views.admin_settings_sound_preview_view, name='admin_settings_sound_preview'),
    path('admin/sms/', views.admin_sms_view, name='admin_sms'),
    path('admin/whatsapp/', views.admin_whatsapp_view, name='admin_whatsapp'),
    path('webhook/whatsapp/', views.whatsapp_webhook_view, name='whatsapp_webhook'),
    path('admin/facebook/', views.admin_facebook_view, name='admin_facebook'),
    path('webhook/facebook/', views.facebook_webhook_view, name='facebook_webhook'),
    path('admin/settings/blacklist/', views.admin_settings_blacklist_view, name='admin_settings_blacklist'),
    path('admin/settings/whatsapp/', views.admin_settings_whatsapp_view, name='admin_settings_whatsapp'),
    path('admin/settings/facebook/', views.admin_settings_facebook_view, name='admin_settings_facebook'),
    path('admin/monitoring/', views.admin_monitoring_view, name='admin_monitoring'),
    path('admin/monitoring/<str:ext>/force-logout/', views.admin_force_logout_view, name='admin_force_logout'),
    path('admin/leads/', views.admin_leads_view, name='admin_leads'),
    path('admin/leads/<int:list_id>/', views.admin_lead_list_view, name='admin_lead_list'),

]

# manage.py runserver auto-serves STATIC_URL via a wrapper around the dev server; running the
# app directly through uvicorn (needed for WebSocket/lifespan support -- see config/asgi.py)
# bypasses that, so static files need an explicit URL pattern here instead.
urlpatterns += staticfiles_urlpatterns()
