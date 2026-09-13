"""
ASGI config for config project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

import core.routing  # noqa: E402
from ami.ami import start_ami_listener, stop_ami_listener  # noqa: E402


class LifespanApp:
    """Starts the AMI listener (see ami/ami.py) when Daphne boots, stops it on shutdown --
    keeps it in the same process as the web server instead of a separate systemd service."""

    async def __call__(self, scope, receive, send):
        while True:
            message = await receive()
            if message['type'] == 'lifespan.startup':
                await start_ami_listener()
                await send({'type': 'lifespan.startup.complete'})
            elif message['type'] == 'lifespan.shutdown':
                await stop_ami_listener()
                await send({'type': 'lifespan.shutdown.complete'})
                return


application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': AuthMiddlewareStack(
        URLRouter(core.routing.websocket_urlpatterns)
    ),
    'lifespan': LifespanApp(),
})
