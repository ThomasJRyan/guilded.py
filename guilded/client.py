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

import asyncio
import logging
import sys
import traceback
from typing import Any, Callable, Optional, List

import aiohttp

from .errors import NotFound, ClientException
from .embed import Embed
from .emoji import Emoji
from .gateway import GuildedWebSocket, UserbotGuildedWebSocket, WebSocketClosure
from .http import HTTPClient, UserbotHTTPClient
from .presence import Presence
from .status import TransientStatus, Game
from .team import Team
from .user import ClientUser, User, Member

log = logging.getLogger(__name__)

__all__ = (
    'Client',
    'UserbotClient',
)


def _cancel_tasks(loop):
    try:
        task_retriever = asyncio.Task.all_tasks
    except AttributeError:
        # future proofing for 3.9 I guess
        task_retriever = asyncio.all_tasks

    tasks = {t for t in task_retriever(loop=loop) if not t.done()}

    if not tasks:
        return

    log.info('Cleaning up after %d tasks.', len(tasks))
    for task in tasks:
        task.cancel()

    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
    log.info('All tasks finished cancelling.')

    for task in tasks:
        if task.cancelled():
            continue
        if task.exception() is not None:
            loop.call_exception_handler({
                'message': 'Unhandled exception during Client.run shutdown.',
                'exception': task.exception(),
                'task': task
            })


def _cleanup_loop(loop):
    try:
        _cancel_tasks(loop)
        if sys.version_info >= (3, 6):
            loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        log.info('Closing the event loop.')
        loop.close()


class _ClientEventTask(asyncio.Task):
    def __init__(self, original_coro, event_name, coro, *, loop):
        super().__init__(coro, loop=loop)
        self.__event_name = event_name
        self.__original_coro = original_coro

    def __repr__(self):
        info = [
            ('state', self._state.lower()),
            ('event', self.__event_name),
            ('coro', repr(self.__original_coro)),
        ]
        if self._exception is not None:
            info.append(('exception', repr(self._exception)))
        return '<ClientEventTask {}>'.format(' '.join('%s=%s' % t for t in info))


class ClientBase:
    def __init__(self, **options):
        # internal
        self.loop = options.pop('loop', asyncio.get_event_loop())
        self.max_messages: int = options.pop('max_messages', 1000)
        self._listeners = {}

        # state
        self._closed = False
        self._ready = asyncio.Event()

    @property
    def cached_messages(self):
        return list(self.http._messages.values())

    @property
    def emojis(self):
        return list(self.http._emojis.values())

    @property
    def teams(self):
        return list(self.http._teams.values())

    @property
    def users(self):
        return list(self.http._users.values())

    @property
    def members(self):
        return list(self.http._team_members.values())

    @property
    def dm_channels(self):
        """The private/dm channels that the connected client can see.

        Returns
        ---------
        List[:class:`.DMChannel`]
        """
        return list(self.http._dm_channels.values())

    @property
    def private_channels(self):
        """|dpyattr|

        This is an alias of :attr:`.dm_channels`.

        Returns
        ---------
        List[:class:`DMChannel`]
        """
        return self.dm_channels

    @property
    def team_channels(self):
        """The team channels that the connected client can see.

        Returns
        ---------
        List[:class:`TeamChannel`]
        """
        return list(self.http._team_channels.values())

    @property
    def channels(self):
        """The channels (Team and DM included) that the connected client can see.

        Returns
        ---------
        List[Union[:class:`TeamChannel`, :class:`DMChannel`]]
        """
        return [*self.dm_channels, *self.team_channels]

    @property
    def guilds(self):
        """|dpyattr|

        This is an alias of :attr:`.teams`.

        Returns
        ---------
        List[:class:`Team`]
        """
        return self.teams

    @property
    def latency(self):
        return float('nan') if self.ws is None else self.ws.latency

    @property
    def closed(self):
        return self._closed

    def is_ready(self):
        return self._ready.is_set()

    async def wait_until_ready(self):
        """|coro|

        Waits until the client's internal cache is all ready.
        """
        await self._ready.wait()

    def wait_for(
        self,
        event: str,
        *,
        check: Optional[Callable[..., bool]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """|coro|

        Waits for a WebSocket event to be dispatched.

        This could be used to wait for a user to reply to a message,
        or to react to a message, or to edit a message in a self-contained
        way.

        The ``timeout`` parameter is passed onto :func:`asyncio.wait_for`. By
        default, it does not timeout. Note that this does propagate the
        :exc:`asyncio.TimeoutError` for you in case of timeout and is provided
        for ease of use.

        In case the event returns multiple arguments, a :class:`tuple`
        containing those arguments is returned instead. Please check the
        :ref:`documentation <guilded-api-events>` for a list of events and
        their parameters.

        This function returns the **first event that meets the requirements**.

        Examples
        ---------

        Waiting for a user reply: ::

            @client.event
            async def on_message(message):
                if message.content.startswith('$greet'):
                    channel = message.channel
                    await channel.send('Say hello!')

                    def check(m):
                        return m.content == 'hello' and m.channel == channel

                    msg = await client.wait_for('message', check=check)
                    await channel.send(f'Hello {msg.author}!')

        Waiting for a thumbs up reaction from the message author: ::

            @client.event
            async def on_message(message):
                if message.content.startswith('$thumb'):
                    channel = message.channel
                    await channel.send('Send me that \N{THUMBS UP SIGN} reaction, mate')

                    def check(reaction, user):
                        return user == message.author and str(reaction.emoji) == '\N{THUMBS UP SIGN}'

                    try:
                        reaction, user = await client.wait_for('reaction_add', timeout=60.0, check=check)
                    except asyncio.TimeoutError:
                        await channel.send('\N{THUMBS DOWN SIGN}')
                    else:
                        await channel.send('\N{THUMBS UP SIGN}')


        Parameters
        ------------
        event: :class:`str`
            The event name, similar to the :ref:`event reference <guilded-api-events>`,
            but without the ``on_`` prefix, to wait for.
        check: Optional[Callable[..., :class:`bool`]]
            A predicate to check what to wait for. The arguments must meet the
            parameters of the event being waited for.
        timeout: Optional[:class:`float`]
            The number of seconds to wait before timing out and raising
            :exc:`asyncio.TimeoutError`.

        Raises
        -------
        asyncio.TimeoutError
            If a timeout is provided and it was reached.

        Returns
        --------
        Any
            Returns no arguments, a single argument, or a :class:`tuple` of multiple
            arguments that mirrors the parameters passed in the
            :ref:`event reference <guilded-api-events>`.
        """

        print(event)
        future = self.loop.create_future()
        if check is None:
            def _check(*args):
                return True
            check = _check

        ev = event.lower()
        try:
            listeners = self._listeners[ev]
        except KeyError:
            listeners = []
            self._listeners[ev] = listeners

        listeners.append((future, check))
        print(self._listeners.get(ev))
        return asyncio.wait_for(future, timeout)

    async def _run_event(self, coro, event_name, *args, **kwargs):
        try:
            await coro(*args, **kwargs)
        except asyncio.CancelledError:
            pass
        except Exception:
            try:
                await self.on_error(event_name, *args, **kwargs)
            except asyncio.CancelledError:
                pass

    def _schedule_event(self, coro, event_name, *args, **kwargs):
        wrapped = self._run_event(coro, event_name, *args, **kwargs)
        # Schedules the task
        return _ClientEventTask(original_coro=coro, event_name=event_name, coro=wrapped, loop=self.loop)

    def event(self, coro):
        """A decorator to register an event for the library to automatically dispatch when appropriate.

        You can find more info about the events on the :ref:`documentation below <guilded-api-events>`.

        The events must be a :ref:`coroutine <coroutine>`, if not, :exc:`TypeError` is raised.

        Example
        ---------

        .. code-block:: python3

            @client.event
            async def on_ready():
                print('Ready!')

        Raises
        --------
        :class:`TypeError`
            The function passed is not actually a coroutine.
        """

        if not asyncio.iscoroutinefunction(coro):
            raise TypeError('Event must be a coroutine.')

        setattr(self, coro.__name__, coro)
        log.debug('%s has successfully been registered as an event', coro.__name__)
        return coro

    def dispatch(self, event: str, *args: Any, **kwargs: Any):
        log.debug('Dispatching event %s', event)
        method = 'on_' + event

        listeners = self._listeners.get(event)
        if listeners:
            removed = []
            for i, (future, condition) in enumerate(listeners):
                if future.cancelled():
                    removed.append(i)
                    continue

                try:
                    result = condition(*args)
                except Exception as exc:
                    future.set_exception(exc)
                    removed.append(i)
                else:
                    if result:
                        if len(args) == 0:
                            future.set_result(None)
                        elif len(args) == 1:
                            future.set_result(args[0])
                        else:
                            future.set_result(args)
                        removed.append(i)

            if len(removed) == len(listeners):
                self._listeners.pop(event)
            else:
                for idx in reversed(removed):
                    del listeners[idx]

        try:
            coro = getattr(self, method)
        except AttributeError:
            pass
        else:
            self._schedule_event(coro, method, *args, **kwargs)

    def get_message(self, id: str):
        """Optional[:class:`.ChatMessage`]: Get a message from your :attr:`.cached_messages`. 
        As messages are often frequently going in and out of cache, you should
        not rely on this method, and instead use :meth:`~.abc.Messageable.fetch_message`.
        
        Parameters
        ------------
        id: :class:`str`
            The ID of the message.
        """
        return self.http._get_message(id)

    def get_team(self, id: str):
        """Optional[:class:`.Team`]: Get a team from your :attr:`.teams`.

        Parameters
        ------------
        id: :class:`str`
            The ID of the team.
        """
        return self.http._get_team(id)

    def get_user(self, id: str):
        """Optional[:class:`guilded.User`]: Get a user from your :attr:`.users`.

        Parameters
        ------------
        id: :class:`str`
            The ID of the user.
        """
        return self.http._get_user(id)

    def get_channel(self, id: str):
        """Optional[:class:`~.abc.Messageable`]: Get a team or DM channel from
        your :attr:`.channels`.

        Parameters
        ------------
        id: :class:`str`
            The ID of the team or dm channel.
        """
        return self.http._get_global_team_channel(id) or self.http._get_dm_channel(id)

    def get_emoji(self, id: int) -> Optional[Emoji]:
        """Optional[:class:`.Emoji`]: Get an emoji from your :attr:`.emojis`.

        Parameters
        ------------
        id: :class:`str`
            The ID of the emoji.
        """
        for team in self.teams:
            emoji = team.get_emoji(id)
            if emoji:
                return emoji

        return None

    async def fetch_team(self, id: str):
        """|coro|

        Fetch a team from the API.

        Returns
        ---------
        :class:`.Team`
            The team from the ID
        """
        team = await self.http.get_team(id)
        return Team(state=self.http, data=team)

    async def getch_team(self, id: str):
        """|coro|

        Try to get a team from internal cache, and if not found, try to fetch from the API.
        
        Returns
        ---------
        :class:`.Team`
            The team from the ID
        """
        return self.get_team(id) or await self.fetch_team(id)

    async def on_error(self, event_method, *args, **kwargs):
        print(f'Ignoring exception in {event_method}:', file=sys.stderr)
        traceback.print_exc()


class UserbotClient(ClientBase):
    """The basic client class for interfacing with Guilded.

    Parameters
    ----------
    max_messages: Optional[:class:`int`]
        The maximum number of messages to store in the internal message cache.
        This defaults to ``1000``. Passing in ``None`` disables the message cache.
    loop: Optional[:class:`asyncio.AbstractEventLoop`]
        The :class:`asyncio.AbstractEventLoop` to use for asynchronous operations.
        Defaults to ``None``, in which case the default event loop is used via
        :func:`asyncio.get_event_loop()`.
    disable_team_websockets: Optional[:class:`bool`]
        Whether to prevent the library from opening team-specific websocket
        connections.
    presence: Optional[:class:`.Presence`]
        A presence to use upon logging in.
    status: Optional[:class:`.TransientStatus`]
        A status (game) to use upon logging in.
    cache_on_startup: Optional[Dict[:class:`str`, :class:`bool`]
        A mapping of types of objects to a :class:`bool` (whether to
        cache the type upon logging in via REST). Currently accepts
        ``members``, ``channels``, ``dm_channels``, ``groups``,
        and ``flowbots``.
        By default, all are enabled.

    Attributes
    -----------
    loop: :class:`asyncio.AbstractEventLoop`
        The event loop that the client uses for HTTP requests and websocket
        operations.
    user: :class:`.ClientUser`
        The currently logged-in user.
    cache_on_startup: Dict[:class:`str`, :class:`bool`]
        The values passed to the ``cache_on_startup`` parameter, each
        defaulting to ``True`` if not specified.
    """
    def __init__(self, **options):
        super().__init__(**options)

        self.http: UserbotHTTPClient = UserbotHTTPClient(
            session=aiohttp.ClientSession(loop=self.loop),
            max_messages=self.max_messages
        )
        self.user: Optional[ClientUser] = None

        self.disable_team_websockets: bool = options.pop('disable_team_websockets', False)
        self._login_presence = options.pop('presence', None)
        self._login_status = options.pop('status', None)

        cache_on_startup = options.pop('cache_on_startup', {})
        self.cache_on_startup = {
            'members': cache_on_startup.get('members', True),
            'channels': cache_on_startup.get('channels', True),
            'dm_channels': cache_on_startup.get('dm_channels', True),
            'groups': cache_on_startup.get('groups', True),
            'flowbots': cache_on_startup.get('flowbots', True)
        }

    async def start(self, email, password, *, reconnect=True):
        """|coro|

        Login and connect to Guilded using a user account email and password.
        """
        await self.login(email, password)
        await self.connect()

    async def login(self, email, password):
        """|coro|

        Log into the REST API with a user account email and password.

        This method fills the internal cache according to
        :attr:`.cache_on_startup`\. This is in contrast to a Discord
        application, where this would be done on connection to the gateway.
        """
        self.http: UserbotHTTPClient = self.http or UserbotHTTPClient(session=aiohttp.ClientSession(loop=self.loop), max_messages=self.max_messages)
        data = await self.http.login(email, password)
        self.user: ClientUser = ClientUser(state=self.http, data=data)
        self.http.my_id = self.user.id

        for team_data in data.get('teams', []):
            team = Team(state=self.http, data=team_data)

            if self.cache_on_startup['members'] is True:
                # We have to re-fetch the team here because member data is not
                # included in the team objects returned in GET /me
                team = await self.fetch_team(team.id)

                # Because of the extra data this endpoint returns, we also
                # want to add this team to our cache, which Team.fetch_members
                # does not do

            if self.cache_on_startup['channels'] is True:
                channels = await team.fetch_channels()
                for channel in channels:
                    if channel is None:
                        continue
                    self.http.add_to_team_channel_cache(channel)

            if self.cache_on_startup['groups'] is True:
                groups = await team.fetch_groups()
                for group in groups:
                    team._groups[group.id] = group

            self.http.add_to_team_cache(team)

        for emoji_data in data.get('customReactions', []):
            emoji = Emoji(data=emoji_data, state=self.http)
            emoji.team._emojis[emoji.id] = emoji

        if self.cache_on_startup['dm_channels'] is True:
            dm_channels = await self.fetch_dm_channels()
            for dm_channel in dm_channels:
                self.http.add_to_dm_channel_cache(dm_channel)
                if dm_channel.recipient is not None:
                    dm_channel.recipient.dm_channel = dm_channel

    async def connect(self):
        """|coro|

        Connect to the main Guilded gateway and subsequent team gateways for
        team-specific events.

        You must log into the REST API (:meth:`login`) before calling this
        method due to the required authentication data that is automatically
        collected in that method.
        """
        if not self.http:
            raise ClientException('You must log in via REST before connecting to the gateway.')

        while not self.closed:
            ws_build = UserbotGuildedWebSocket.build(self, loop=self.loop)
            gws = await asyncio.wait_for(ws_build, timeout=60)
            if type(gws) != UserbotGuildedWebSocket:
                self.dispatch('error', gws)
                return

            self.ws = gws
            self.http.ws = self.ws
            self.dispatch('connect')

            if self._login_presence is not None:
                # we do this here because why bother setting a presence if you won't show up in the online list anyway
                await self.change_presence(self._login_presence)

            #if self._login_presence is Presence.online:
            # todo: start http ping thread
            # no need to do that if you don't want an online presence

            if not self.disable_team_websockets:
                for team in self.teams:
                    team_ws_build = UserbotGuildedWebSocket.build(self, loop=self.loop, teamId=team.id)
                    team_ws = await asyncio.wait_for(team_ws_build, timeout=60)
                    if type(team_ws) == UserbotGuildedWebSocket:
                        team.ws = team_ws
                        self.dispatch('team_connect', team)

            async def listen_socks(ws, team=None):
                teamId = team.id if team is not None else None
                next_backoff_time = 5
                while True and ws is not None:
                    try:
                        await ws.poll_event()
                    except WebSocketClosure as exc:
                        code = ws._close_code or ws.socket.close_code
                        if teamId:
                            log.warning('Team %s\'s websocket closed with code %s, attempting to reconnect in %s seconds', teamId, code, next_backoff_time)
                            self.dispatch('team_disconnect', teamId)
                        else:
                            log.warning('Websocket closed with code %s, attempting to reconnect in %s seconds', code, next_backoff_time)
                            self.dispatch('disconnect')
                        await asyncio.sleep(next_backoff_time)
                        if teamId:
                            build = UserbotGuildedWebSocket.build(self, loop=self.loop, teamId=teamId)
                        else:
                            # possible reconnect issues brought up by @8r2y5
                            build = UserbotGuildedWebSocket.build(self, loop=self.loop)
                        try:
                            ws = await asyncio.wait_for(build, timeout=60)
                        except asyncio.TimeoutError:
                            log.warning('Timed out trying to reconnect.')
                            next_backoff_time += 5
                    else:
                        next_backoff_time = 5

            self._ready.set()
            self.dispatch('ready')

            await asyncio.gather(
                listen_socks(self.ws), *[listen_socks(team.ws, team) for team in self.teams]
            )

    async def close(self):
        """|coro|

        Log out and close any active gateway connections.
        """
        if self._closed: return

        await self.http.logout()
        for ws in [self.ws] + [team.ws for team in self.teams if team.ws is not None]:
            try:
                await ws.close(code=1000)
            except Exception:
                # it's probably already closed, but catch all anyway
                pass

        self._closed = True
        self._ready.clear()

    def run(self, email: str, password: str):
        """Login and connect to Guilded, and start the event loop. This is a
        blocking call, nothing after it will be called until the bot has been
        closed.
        """
        try:
            self.loop.create_task(self.start(
                email=email, 
                password=password
            ))
            self.loop.run_forever()
        except KeyboardInterrupt:
            exit()

    async def search_users(self, query: str, *, max_results=20, exclude=None):
        """|coro|

        Search Guilded for users. Returns an array of partial users.

        Parameters
        ------------
        query: :class:`str`
            Query to use while searching
        max_results: Optional[:class:`int`]
            The maximum number of results to return. Defaults to 20.
        exclude: Optional[List[:class:`guilded.User`]]
            A list of users to exclude from results. A common value for this
            could be your list of :attr:`.users`.

        Returns
        ---------
        List[:class:`guilded.User`]
            The users from the query
        """
        results = await self.http.search(query,
            entity_type='user',
            max_results=max_results,
            exclude=[item.id for item in (exclude or [])]
        )
        users = []
        for user_data in results['results']['users']:
            users.append(self.http.create_user(data=user_data))

        return users

    async def search_teams(self, query: str, *, max_results=20, exclude=None):
        """|coro|

        Search Guilded for public teams. Returns an array of partial teams.

        Parameters
        ------------
        query: :class:`str`
            Query to use while searching
        max_results: Optional[:class:`int`]
            The maximum number of results to return. Defaults to 20.
        exclude: Optional[List[:class:`.Team`]]
            A list of teams to exclude from results. A common value for this
            could be your list of :attr:`.teams`.

        Returns
        ---------
        List[:class:`.Team`]
            The teams from the query
        """
        results = await self.http.search(query,
            entity_type='team',
            max_results=max_results,
            exclude=[item.id for item in (exclude or [])]
        )
        teams = []
        for team_object in results['results']['teams']:
            team_object['isPublic'] = True  # These results will only have public teams, however this attribute 
                                            # is not present in each object, so this compensates
            teams.append(Team(state=self.http, data=team_object))

        return teams

    async def join_team(self, id: str):
        """|coro|

        Join a public team using its ID.

        Returns
        ---------
        :class:`.Team`
            The team you joined from the ID
        """
        await self.http.join_team(id)
        team = await self.http.get_team(id)
        return

    async def fetch_user(self, id: str):
        """|coro|

        Fetch a user from the API.

        Returns
        ---------
        :class:`guilded.User`
            The user from the ID
        """
        user = await self.http.get_user(id)
        return User(state=self.http, data=user)

    async def getch_user(self, id: str):
        """|coro|

        Try to get a user from internal cache, and if not found, try to fetch from the API.
        
        Returns
        ---------
        :class:`guilded.User`
            The user from the ID
        """
        return self.get_user(id) or await self.fetch_user(id)

    async def getch_channel(self, id: str, team_id: str = None):
        """|coro|

        Try to get a channel from internal cache, and if not found, try to fetch from the API.

        Returns
        ---------
        Union[:class:`~.abc.TeamChannel`, :class:`.DMChannel`]
            The channel from the ID
        """
        channel = self.get_channel(id)
        if team_id is None and channel is None:
            raise NotFound(id)
        elif team_id is not None and channel is None:
            team = await self.getch_team(team_id)
            channel = team.get_channel(id) or await team.fetch_channel(id)

        return channel

    async def fetch_game(self, id: int) -> Game:
        """|coro|

        Fetch a game from the
        `documentation's game list <https://guildedapi.com/resources/user#game-ids>`_\.

        Games not in this list will be considered invalid by Guilded.
        
        Returns
        ---------
        :class:`.Game`
            The game from the ID
        """
        if not Game.MAPPING:
            await self.fill_game_list()

        game = Game(game_id=id)
        if game.name is None:
            raise ValueError(f'{id} is not a recognized game id.')

        return game

    async def fetch_games(self):
        """|coro|

        Fetch the list of documented games that Guilded supports.

        Returns
        ---------
        :class:`dict`
            The whole game list (`viewable here <https://github.com/GuildedAPI/datatables/blob/main/games.json>`_)
        """
        return await self.http.get_game_list()

    async def fill_game_list(self):
        """|coro|

        Fill the internal game list cache from remote data.
        """
        games = await self.fetch_games()
        Game.MAPPING = games

    async def update_privacy_settings(self, *, dms, friend_requests):
        """|coro|

        Update your privacy settings.
        """
        await self.http.set_privacy_settings(dms=dms, friend_requests=friend_requests)

    async def fetch_blocked_users(self):
        """|coro|

        The users the client has blocked.

        Returns
        ---------
        List[:class:`guilded.User`]
        """
        settings = await self.http.get_privacy_settings()
        blocked = []
        for user in settings.get('blockedUsers', []):
            blocked.append(User(state=self.http, data=user))

        return blocked

    async def set_presence(self, presence: Presence):
        """|coro|

        Set your presence.

        "Presence" in a Guilded context refers to the colored circle next to
        your profile picture, as opposed to the equivalent "status" concept in
        the Discord API.

        Parameters
        -----------
        presence: :class:`.Presence`
            The presence to use.
        """
        if presence is None:
            presence = Presence.online
        if isinstance(presence, Presence):
            await self.http.set_presence(presence.value)
        else:
            raise TypeError('presence must be of type Presence or be None, not %s' % presence.__class__.__name__)

    async def set_status(self, status: TransientStatus):
        """|coro|

        Set your status.

        "Status" in a Guilded context refers to your custom status or game
        activity rather than the colored circle next to your profile picture.

        Parameters
        -----------
        status: :class:`.TransientStatus`
            The transient status to use.
        """
        if isinstance(status, Game):
            await self.http.set_transient_status(status.game_id)
        elif isinstance(status, TransientStatus):
            await self.http.set_custom_status(status.name)
        elif status is None:
            await self.http.delete_transient_status()
        else:
            raise TypeError('status must inherit from TransientStatus (Game, CustomStatus) or be None, not %s' % status.__class__.__name__)

    async def change_presence(self, **kwargs):
        """|coro|
        
        Change your presence.
        
        This method exists only for backwards compatibility with discord.py
        bots. When writing new bots, it is generally preferred to use the
        :meth:`set_presence` and :meth:`set_status` methods instead.

        Parameters
        -----------
        status: Optional[:class:`.Presence`]
            The presence to display. If ``None``, :attr:`Presence.online` is
            used.
        activity: Optional[:class:`.TransientStatus`]
            The activity to display. If ``None``, no activity will be
            displayed.
        """
        try:
            presence = kwargs.pop('status')
            await self.set_presence(presence)
        except KeyError:
            pass

        try:
            status = kwargs.pop('activity')
            await self.set_status(status)
        except KeyError:
            pass

    async def fetch_subdomain(self, subdomain: str):
        """|coro|

        Check if a subdomain (profile or team vanity url) is available.

        Returns
        --------
        Optional[Union[:class:`guilded.User`, :class:`.Team`]]
            The user or team that is using this subdomain.
        """
        value = await self.http.check_subdomain(subdomain)
        if (value or {}).get('exists') is True:
            # currently this endpoint returns {} if the subdomain does not
            # exist, but just in case it eventually returns 204 or something,
            # we check more explicitly instead.
            if value.get('teamId'):
                using_subdomain = await self.getch_team(value.get('teamId'))
            elif value.get('userId'):
                using_subdomain = await self.getch_user(value.get('userId'))

            return using_subdomain

        else:
            return None

    async def fetch_metadata(self, route: str):
        """|coro|

        This is essentially a barebones wrapper method for the Get Metadata
        endpoint; it returns raw JSON data from the metadata route provided.

        Returns
        --------
        :class:`dict`
        """
        data = await self.http.get_metadata(route)
        return data

    async def fetch_link_embed(self, url: str):
        """|coro|

        Fetch the OpenGraph data for a URL. The request made here is to
        Guilded, not directly to the webpage, so any images will be proxy
        URLs.

        Returns
        --------
        :class:`Embed`
        """
        data = await self.http.get_embed_for_url(url)
        embed = Embed.from_unfurl_dict(data)
        return embed

    async def fetch_referrals(self):
        """|coro|

        Get your referral statistics.
        """
        data = await self.http.get_referral_statistics()

    async def fetch_dm_channels(self):
        """|coro|

        Fetch your DM channels.

        This endpoint will only return channels that you have not "hidden",
        i.e. called :meth:`DMChannel.hide` on.
        """
        data = await self.http.get_dm_channels()
        channels = []
        for dm_channel_data in data.get('channels', data):
            dm_channel = self.http.create_channel(data=dm_channel_data)
            channels.append(dm_channel)

        return channels

    async def fetch_emojis(self):
        """|coro|

        Fetch your emojis.
        """
        data = await self.http.get_emojis()
        emojis = []
        for emoji_data in data['customReactions']:
            team = self.get_team(emoji_data['teamId'])
            emoji = Emoji(team=team, data=emoji_data, state=self.http)
            emojis.append(emoji)

        return emojis


class Client(ClientBase):
    """The basic client class for interfacing with Guilded.

    Parameters
    ----------
    user_id: :class:`str`
        The user ID of this bot, copied from the "Bots" menu. This is used to
        check if a message is owned by the client, and will be removed in the
        future. This is also used to map the client's :class:`.Member` object
        to attributes like :attr:`.Team.me`.
    team_id: Optional[:class:`str`]
        The ID of the team that this bot is "from".
    max_messages: Optional[:class:`int`]
        The maximum number of messages to store in the internal message cache.
        This defaults to ``1000``. Passing in ``None`` disables the message cache.
    loop: Optional[:class:`asyncio.AbstractEventLoop`]
        The :class:`asyncio.AbstractEventLoop` to use for asynchronous operations.
        Defaults to ``None``, in which case the default event loop is used via
        :func:`asyncio.get_event_loop()`.

    Attributes
    -----------
    loop: :class:`asyncio.AbstractEventLoop`
        The event loop that the client uses for HTTP requests and websocket
        operations.
    user: :class:`.ClientUser`
        The currently logged-in user.
    team: :class:`.Team`
        The team that this bot is "from".
    ws: Optional[:class:`GuildedWebsocket`]
        The websocket gateway the client is currently connected to. Could be
        ``None``.
    """

    def __init__(self, *, user_id: str, team_id: str = None, **options):
        super().__init__(**options)
        self.user_id = user_id
        self.team_id = team_id

        self.http: HTTPClient = HTTPClient(
            session=aiohttp.ClientSession(),
            user_id=user_id,
            max_messages=self.max_messages
        )
        self.user: ClientUser = ClientUser(data={'id': user_id}, state=self.http)
        team = Team(data={'id': team_id}, state=self.http)
        team._members[self.user.id] = self.http.create_member(team=team, data={
            'id': self.user.id,
            'name': self.user.name,
            'bot': True,
        })
        self.http.add_to_team_cache(team)

    async def start(self, token=None, *, reconnect=True):
        self.http.token = token or self.http.token
        if not self.http.token:
            raise ClientException(
                'You must provide a token to this method explicitly, or have '
                'it already set in this Client\'s HTTPClient beforehand.'
            )

        # build the cache of our one team
        if self.team_id:
            team = await self.fetch_team(self.team_id)
            team._members[self.user.id] = self.http.create_member(team=team, data={
                'id': self.user.id,
                'name': self.user.name,
                'bot': True,
            })
            self.http.add_to_team_cache(team)

        await self.connect(token, reconnect=reconnect)

    async def connect(self, token=None, *, reconnect=True):
        self.http.token = token or self.http.token
        if not self.http.token:
            raise ClientException(
                'You must provide a token to this method explicitly, or have '
                'it already set in this Client\'s HTTPClient beforehand.'
            )

        while not self.closed:
            ws_build = GuildedWebSocket.build(self, loop=self.loop)
            gws = await asyncio.wait_for(ws_build, timeout=60)
            if type(gws) != GuildedWebSocket:
                self.dispatch('error', gws)
                return

            self.ws = gws
            self.http.ws = self.ws
            self.dispatch('connect')

            async def listen_socks(ws):
                next_backoff_time = 5
                while True and ws is not None:
                    try:
                        await ws.poll_event()
                    except WebSocketClosure as exc:
                        code = ws._close_code or ws.socket.close_code

                        if reconnect is False:
                            log.warning('Websocket closed with code %s. Last message ID was %s', code, ws._last_message_id)
                            await self.close()
                            break

                        log.warning('Websocket closed with code %s, attempting to reconnect in %s seconds with last message ID %s', code, next_backoff_time, ws._last_message_id)
                        self.dispatch('disconnect')
                        await asyncio.sleep(next_backoff_time)

                        build = GuildedWebSocket.build(self, loop=self.loop)
                        try:
                            ws = await asyncio.wait_for(build, timeout=60)
                        except asyncio.TimeoutError:
                            log.warning('Timed out trying to reconnect.')
                            next_backoff_time += 5
                    else:
                        next_backoff_time = 5

            self._ready.set()
            self.dispatch('ready')
            await listen_socks(self.ws)

    async def close(self):
        """|coro|

        Close the current connection.
        """
        if self._closed: return

        try:
            await ws.close(code=1000)
        except Exception:
            # it's probably already closed, but catch all anyway
            pass

        self._closed = True
        self._ready.clear()

    def run(self, token: str, *, reconnect=True):
        """Connect to Guilded's gateway and start the event loop. This is a
        blocking call; nothing after it will be called until the bot has been
        closed.

        Parameters
        ------------
        token: :class:`str`
            The bot's auth token,
        reconnect: Optional[:class:`bool`]
            Whether to reconnect on loss/interruption of gateway connection.
        """
        try:
            self.loop.create_task(self.start(token, reconnect=reconnect))
            self.loop.run_forever()
        except KeyboardInterrupt:
            exit()
