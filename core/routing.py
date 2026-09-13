from django.urls import path

from core.consumers import RealtimeConsumer

websocket_urlpatterns = [
    path('ws/realtime/', RealtimeConsumer.as_asgi()),
]
