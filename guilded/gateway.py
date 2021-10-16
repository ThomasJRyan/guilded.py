"""
MIT License

Copyright (c) 2020-present shay (shayypy)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

------------------------------------------------------------------------------

This project includes code from https://github.com/Rapptz/discord.py, which is
available under the MIT license:

The MIT License (MIT)

Copyright (c) 2015-present Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

import aiohttp
import asyncio
import concurrent.futures
import datetime
import json
import logging
import sys
import threading
import traceback

from guilded.abc import TeamChannel

from .errors import GuildedException
from .channel import *
from .message import Message
from .presence import Presence
from .user import Member, User

log = logging.getLogger(__name__)


class WebSocketClosure(Exception):
    """An exception to make up for the fact that aiohttp doesn't signal closure."""
    pass

class GuildedWebSocket:
    """Implements Guilded's global gateway as well as team websocket connections."""
    HEARTBEAT_PAYLOAD = '2'
    def __init__(self, socket, client, *, loop):
        self.client = client
        self.loop = loop
        self._heartbeater = None

        # socket
        self.socket = socket
        self._close_code = None
        self.team_id = None

        # gateway hello data
        self.sid = None
        self.upgrades = []

    async def send(self, payload, *, raw=False):
        if raw is False:
            payload = f'42{json.dumps(payload)}'

        self.client.dispatch('socket_raw_send', payload)
        return await self.socket.send_str(payload)

    @property
    def latency(self):
        return float('inf') if self._heartbeater is None else self._heartbeater.latency

    @classmethod
    async def build(cls, client, *, loop=None, **gateway_args):
        log.info('Connecting to the gateway with args %s', gateway_args)
        try:
            socket = await client.http.ws_connect(**gateway_args)
        except aiohttp.client_exceptions.WSServerHandshakeError as exc:
            log.error('Failed to connect: %s', exc)
            return exc
        else:
            log.info('Connected')

        ws = cls(socket, client, loop=loop or asyncio.get_event_loop())
        ws.team_id = gateway_args.get('teamId')
        ws._parsers = WebSocketEventParsers(client)
        await ws.send(GuildedWebSocket.HEARTBEAT_PAYLOAD, raw=True)
        await ws.poll_event()

        return ws

    def _pretty_event(self, payload):
        if isinstance(payload, list):
            payload = payload[1]
        if not payload.get('type'):
            return payload

        return {
            'type': payload.pop('type'),
            'data': {k: v for k, v in payload.items()}
        }

    def _full_event_parse(self, payload):
        for char in payload:
            if char.isdigit():
                payload = payload.replace(char, '', 1)
            else:
                break
        data = json.loads(payload)
        return self._pretty_event(data)

    async def received_event(self, payload):
        if payload.isdigit():
            return

        self.client.dispatch('socket_raw_receive', payload)
        data = self._full_event_parse(payload)
        self.client.dispatch('socket_response', data)
        log.debug('Received %s', data)

        if data.get('sid') is not None:
            # hello
            self.sid = data['sid']
            self.upgrades = data['upgrades']
            self._heartbeater = Heartbeater(ws=self, interval=data['pingInterval'] / 1000)
            # maybe implement timeout later, idk
            
            #await self.send(self.HEARTBEAT_PAYLOAD, raw=True)
            # not sure if a heartbeat should be sent here since it's sent when starting the heartbeater anyway
            # (doing so results in double heartbeat)
            self._heartbeater.start()
            return

        event = self._parsers.get(data['type'], data['data'])
        if event is None:
            # ignore unhandled events
            return
        try:
            await event
        except GuildedException as e:
            self.client.dispatch('error', e)
            raise
        except Exception as e:
            # wrap error if not already from the lib
            exc = GuildedException(e)
            self.client.dispatch('error', exc)
            raise exc from e

    async def poll_event(self):
        msg = await self.socket.receive()
        if msg.type is aiohttp.WSMsgType.TEXT:
            await self.received_event(msg.data)
        elif msg.type is aiohttp.WSMsgType.ERROR:
            raise msg.data
        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSE):
            raise WebSocketClosure('Socket is in a closed or closing state.')
        return None

    async def close(self, code=1000):
        self._close_code = code
        await self.send(['logout'])
        await self.socket.close(code=code)

class GuildedVoiceWebSocket(GuildedWebSocket):
    """Implements websocket connections to Guilded voice channels."""
    def __init__(self, socket, client, *, endpoint, channel_id, token, loop):
        super().__init__(socket, client, loop=loop)

        self.endpoint = endpoint
        self.channel_id = channel_id
        self.token = token

    @classmethod
    async def build(cls, client, *, endpoint, channel_id, token, loop=None):
        log.info('Connecting to the voice gateway %s for channel %s', endpoint, channel_id)
        try:
            socket = await client.http.voice_ws_connect(endpoint, channel_id, token)
        except aiohttp.client_exceptions.WSServerHandshakeError as exc:
            log.error('Failed to connect: %s', exc)
            return exc
        else:
            log.info('Connected')

        ws = cls(socket, client, endpoint=endpoint, channel_id=channel_id, token=token, loop=loop or asyncio.get_event_loop())
        ws._parsers = WebSocketEventParsers(client)
        await ws.send(GuildedWebSocket.HEARTBEAT_PAYLOAD, raw=True)
        await ws.poll_event()

        return ws

class WebSocketEventParsers:
    def __init__(self, client):
        self.client = client
        self._state = client.http

    def get(self, event_name, data):
        coro = getattr(self, event_name, None)
        if not coro:
            return None
        return coro(data)

    async def ChatMessageCreated(self, data):
        channelId = data.get('channelId', data.get('message', {}).get('channelId'))
        teamId = data.get('teamId', data.get('message', {}).get('teamId'))
        createdBy = data.get('createdBy', data.get('message', {}).get('createdBy'))
        channel, author, team = None, None, None

        if channelId is not None:
            try:
                channel = await self.client.getch_channel(channelId)
            except:
                pass

        if teamId is not None:
            if channel:
                team = channel.team
            else:
                try:
                    team = await self.client.getch_team(teamId)
                except:
                    pass

        else:
            author = await self.client.getch_user(createdBy)

        if channel is None:
            if team:
                channel = await team.getch_channel(channelId)
            else:
                dm_channel_data = await self._state.get_channel(channelId)
                channel = self._state.create_channel(data=dm_channel_data['metadata']['channel'])

        message = self._state.create_message(channel=channel, data=data, author=author, team=team)
        self._state.add_to_message_cache(message)
        self.client.dispatch('message', message)

    async def ChatChannelTyping(self, data):
        self.client.dispatch('typing', data['channelId'], data['userId'], datetime.datetime.utcnow())

    async def ChatMessageDeleted(self, data):
        message = self.client.get_message(data['message']['id'])
        data['cached_message'] = message
        self.client.dispatch('raw_message_delete', data)
        if message is not None:
            try:
                self.client.cached_messages.remove(message)
            except:
                pass
            finally:
                self.client.dispatch('message_delete', message)

    async def ChatPinnedMessageCreated(self, data):
        if data.get('channelType') == 'Team':
            self.client.dispatch('raw_team_message_pinned', data)
        else:
            self.client.dispatch('raw_dm_message_pinned', data)
        message = self.client.get_message(data['message']['id'])
        if message is None:
            return
        
        if message.team is not None:
            self.client.dispatch('team_message_pinned', message)
        else:
            self.client.dispatch('dm_message_pinned', message)

    async def ChatPinnedMessageDeleted(self, data):
        if data.get('channelType') == 'Team':
            self.client.dispatch('raw_team_message_unpinned', data)
        else:
            self.client.dispatch('raw_dm_message_unpinned', data)
        message = self.client.get_message(data['message']['id'])
        if message is None:
            return#message = PartialMessage()

        if message.team is not None:
            self.client.dispatch('team_message_unpinned', message)
        else:
            self.client.dispatch('dm_message_unpinned', message)

    async def ChatMessageUpdated(self, data):
        self.client.dispatch('raw_message_edit', data)
        before = self.client.get_message(data['message']['id'])
        if before is None:
            return

        data['webhookId'] = before.webhook_id
        data['createdAt'] = before.created_at.isoformat(timespec='milliseconds') + 'Z'

        after = Message(state=self.client.http, channel=before.channel, author=before.author, data=data)
        self._state.add_to_message_cache(after)
        self.client.dispatch('message_edit', before, after)

    async def TeamXpSet(self, data):
        if not data.get('amount'): return
        team = self.client.get_team(data['teamId'])
        if team is None: return
        before = team.get_member(data['userIds'][0] if data.get('userIds') else data['userId'])
        if before is None: return

        after = team.get_member(before.id)
        after.xp = data['amount']
        self._state.add_to_member_cache(after)
        self.client.dispatch('member_update', before, after)

    async def TeamMemberUpdated(self, data):
        raw_after = Member(state=self._state, data=data)
        self.client.dispatch('raw_member_update', raw_after)

        team = self.client.get_team(data['teamId'])
        if team is None: return
        if data.get('userId'):
            before = team.get_member(data.get('userId'))
        else:
            # probably includes userIds instead, which i don't plan on handling yet
            return
        if before is None:
            return

        for key, val in data['userInfo'].items():
            after = team.get_member(data['userId'])
            setattr(after, key, val)
            self._state.add_to_member_cache(after)

        self.client.dispatch('member_update', before, after)

    async def teamRolesUpdates(self, data):
        try: team = await self.client.getch_team(data['teamId'])
        except: return

        for updated in data['memberRoleIds']:
            before = team.get_member(updated['userId'])
            if not before: continue

            after = team.get_member(before.id)
            after.roles = updated['roleIds']
            self._state.add_to_member_cache(after)
            self.client.dispatch('member_update', before, after)

    async def TemporalChannelCreated(self, data):
        if data.get('channelType', '').lower() == 'team':
            try: team = await self.client.getch_team(data['teamId'])
            except: return

            thread = Thread(state=self._state, group=None, data=data.get('channel', data), team=team)
            self.client.dispatch('team_thread_created', thread)

    async def TeamMemberRemoved(self, data):
        team_id = data.get('teamId')
        user_id = data.get('userId')
        self._state.remove_from_member_cache(team_id, user_id)
        #self.client.dispatch('member_remove', user)

    async def TeamMemberJoined(self, data):
        try: team = await self.client.getch_team(data['teamId'])
        except: team = None
        member = Member(state=self._state, data=data['user'], team=team)
        self._state.add_to_member_cache(member)
        self.client.dispatch('member_join', member)

    async def ChatChannelHidden(self, data):
        channel_id = data['channelId']
        dm_channel = self._state._get_dm_channel(channel_id)
        if dm_channel:
            self.client.dispatch('dm_channel_hide', dm_channel)
            self._state.remove_from_dm_channel_cache(channel_id)

    async def USER_UPDATED(self, data):
        # transient status update handling
        # also happens in TeamMemberUpdated
        # this might just be yourself?
        pass

    async def USER_PRESENCE_MANUALLY_SET(self, data):
        status = data.get('status', 1)
        self.client.user.presence = Presence.from_value(status)
        
        #self.client.dispatch('self_presence_set', self.client.user.presence)
        # not sure if an event should be dispatched for this
        # it happens when you set your own presence

    async def TEAM_CHANNEL_CONTENT_CREATED(self, data):
        try:
            team = await self.client.getch_team(data['teamId'])
        except:
            return
        channel = team.get_channel(data['channelId'])
        if channel is None:
            return

        moved = data.get('contentMoved', False)

        if channel.type is ChannelType.forum:
            content = ForumTopic(data=data['thread'], channel=channel, state=self._state)
            channel._topics[content.id] = content
            if moved:
                self.client.dispatch('forum_topic_move', content)
            else:
                self.client.dispatch('forum_topic_create', content)

        elif channel.type is ChannelType.doc:
            content = Doc(data=data['doc'], channel=channel, state=self._state)
            channel._docs[content.id] = content
            if moved:
                self.client.dispatch('doc_move', content)
            else:
                self.client.dispatch('doc_create', content)

        elif channel.type is ChannelType.announcement:
            content = Announcement(data=data['announcement'], channel=channel, state=self._state)
            channel._announcements[content.id] = content
            self.client.dispatch('announcement_create', content)

    async def TEAM_CHANNEL_CONTENT_DELETED(self, data):
        try:
            team = await self.client.getch_team(data['teamId'])
        except:
            return
        channel = team.get_channel(data['channelId'])
        if channel is None:
            return

        try:
            deleted_by = await team.getch_member(data['deletedBy'])
        except:
            deleted_by = None

        content_id = data['contentId']

        if channel.type is ChannelType.forum:
            self.client.dispatch('raw_forum_topic_delete', channel, int(content_id))
            content = channel.get_topic(int(content_id))
            if topic is not None:
                content.deleted_by = deleted_by
                self.client.dispatch('forum_topic_delete', content)
                channel._topics.pop(content.id)

        elif channel.type is ChannelType.doc:
            self.client.dispatch('raw_doc_delete', channel, int(content_id))
            content = channel.get_doc(int(content_id))
            if content is not None:
                content.deleted_by = deleted_by
                self.client.dispatch('doc_delete', content)
                channel._docs.pop(content.id)

        elif channel.type is ChannelType.announcement:
            self.client.dispatch('raw_announcement_delete', channel, content_id)
            content = channel.get_announcement(content_id)
            if content is not None:
                content.deleted_by = deleted_by
                self.client.dispatch('announcement_delete', content)
                channel._announcements.pop(content.id)

    async def TEAM_CHANNEL_CONTENT_REPLY_CREATED(self, data):
        try:
            team = await self.client.getch_team(data['teamId'])
        except:
            return
        channel = team.get_channel(data['channelId'])
        if channel is None:
            return

        parent_id = data['contentId']

        if channel.type is ChannelType.forum:
            try:
                parent = await channel.getch_topic(int(parent_id))
                channel._topics[parent.id] = parent
            except:
                return
            reply = ForumReply(data=data['reply'], parent=parent, state=self._state)
            reply.parent._replies[reply.id] = reply
            self.client.dispatch('forum_reply_create', reply)

        elif channel.type is ChannelType.doc:
            try:
                parent = await channel.getch_doc(int(parent_id))
                channel._docs[parent.id] = parent
            except:
                return
            reply = DocReply(data=data['reply'], parent=parent, state=self._state)
            parent._replies[reply.id] = reply
            self.client.dispatch('doc_reply_create', reply)

        elif channel.type is ChannelType.announcement:
            try:
                parent = await channel.getch_announcement(parent_id)
                channel._docs[parent.id] = parent
            except:
                return
            reply = AnnouncementReply(data=data['reply'], parent=parent, state=self._state)
            parent._replies[reply.id] = reply
            self.client.dispatch('announcement_reply_create', reply)

    async def TEAM_CHANNEL_CONTENT_REPLY_DELETED(self, data):
        try:
            team = await self.client.getch_team(data['teamId'])
        except:
            return
        channel = team.get_channel(data['channelId'])
        if channel is None:
            return

        try:
            deleted_by = await team.getch_member(data['deletedBy'])
        except:
            deleted_by = None

        parent_id = data['contentId']
        reply_id = int(data['contentReplyId'])

        if channel.type is ChannelType.forum:
            self.client.dispatch('raw_forum_reply_delete', channel, int(parent_id), reply_id)
            try:
                parent = await channel.getch_topic(int(parent_id))
                channel._topics[parent.id] = parent
            except:
                return
            reply = parent.get_reply(reply_id)
            if reply is not None:
                reply.deleted_by = deleted_by
                self.client.dispatch('forum_reply_delete', reply)
                parent._replies.pop(reply.id)

        elif channel.type is ChannelType.doc:
            self.client.dispatch('raw_doc_reply_delete', channel, int(parent_id), reply_id)
            try:
                parent = await channel.getch_doc(int(parent_id))
                channel._docs[parent.id] = parent
            except:
                return
            reply = parent.get_reply(reply_id)
            if reply is not None:
                reply.deleted_by = deleted_by
                self.client.dispatch('doc_reply_delete', reply)
                parent._replies.pop(reply.id)

        elif channel.type is ChannelType.announcement:
            self.client.dispatch('raw_announcement_reply_delete', channel, parent_id, reply_id)
            try:
                parent = await channel.getch_announcement(parent_id)
                channel._announcements[parent.id] = parent
            except:
                return
            reply = parent.get_reply(reply_id)
            if reply is not None:
                reply.deleted_by = deleted_by
                self.client.dispatch('announcement_reply_delete', reply)
                parent._replies.pop(reply.id)


class Heartbeater(threading.Thread):
    def __init__(self, ws, *, interval):
        self.ws = ws
        self.interval = interval
        #self.heartbeat_timeout = timeout
        threading.Thread.__init__(self)

        self.msg = 'Keeping websocket alive with sequence %s.'
        self.block_msg = 'Websocket heartbeat blocked for more than %s seconds.'
        self.behind_msg = 'Can\'t keep up, websocket is %.1fs behind.'
        self._stop_ev = threading.Event()
        self.latency = float('inf')

    def run(self):
        log.debug('Started heartbeat thread')
        while not self._stop_ev.wait(self.interval):
            log.debug('Sending heartbeat')
            coro = self.ws.send(GuildedWebSocket.HEARTBEAT_PAYLOAD, raw=True)
            f = asyncio.run_coroutine_threadsafe(coro, loop=self.ws.loop)
            try:
                total = 0
                while True:
                    try:
                        f.result(10)
                        break
                    except concurrent.futures.TimeoutError:
                        total += 10
                        try:
                            frame = sys._current_frames()[self._main_thread_id]
                        except KeyError:
                            msg = self.block_msg
                        else:
                            stack = traceback.format_stack(frame)
                            msg = '%s\nLoop thread traceback (most recent call last):\n%s' % (self.block_msg, ''.join(stack))
                        log.warning(msg, total)

            except Exception:
                self.stop()

    def stop(self):
        self._stop_ev.set()
