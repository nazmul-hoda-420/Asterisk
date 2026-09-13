import json

from channels.generic.websocket import AsyncWebsocketConsumer


class RealtimeConsumer(AsyncWebsocketConsumer):
    """The app's one shared live-update channel -- agent/channel status for the admin
    Monitoring page and the agent-facing team status panel, queue updates, broadcast
    progress, and account-level events like session_kicked all go through this single
    connection, distinguished only by each message's 'kind' field (see ami/ami.py and
    core/views.py for the senders). One connection instead of a separate WebSocket per
    feature keeps this simple: a new live-update feature is just a new 'kind', not a new
    route/consumer. None of it is sensitive, so any authenticated user (admin or agent)
    can connect."""
    group_name = 'realtime'

    async def connect(self):
        user = self.scope['user']
        if not user.is_authenticated:
            await self.close()
            return
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def realtime_update(self, event):
        await self.send(text_data=json.dumps(event['data']))
