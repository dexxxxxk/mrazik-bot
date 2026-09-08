import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
import discord
from features import resolve_text_channel,TextDestination
from game import GameError


class ChannelFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_channel_survives_transformation_then_fetches(self):
        partial=SimpleNamespace(id=10,resolve=lambda:None)
        value=await TextDestination().transform(None,partial)
        channel=Mock(spec=discord.TextChannel);channel.guild=SimpleNamespace(id=1)
        client=SimpleNamespace(get_channel=Mock(return_value=None),fetch_channel=AsyncMock(return_value=channel))
        self.assertIs(await resolve_text_channel(client,1,value.id),channel)
        client.fetch_channel.assert_awaited_once_with(10)

    async def test_cached_channel_needs_no_http(self):
        channel=Mock(spec=discord.TextChannel);channel.guild=SimpleNamespace(id=1)
        client=SimpleNamespace(get_channel=Mock(return_value=channel),fetch_channel=AsyncMock())
        self.assertIs(await resolve_text_channel(client,1,10),channel)
        client.fetch_channel.assert_not_awaited()

    async def test_missing_access_has_actionable_message(self):
        response=SimpleNamespace(status=403,reason='Forbidden')
        client=SimpleNamespace(get_channel=Mock(return_value=None),fetch_channel=AsyncMock(side_effect=discord.Forbidden(response,'Missing Access')))
        with self.assertRaisesRegex(GameError,'Просматривать канал'):await resolve_text_channel(client,1,10)

    async def test_wrong_type_and_other_guild_are_rejected(self):
        client=SimpleNamespace(get_channel=Mock(return_value=Mock(spec=discord.VoiceChannel)))
        with self.assertRaisesRegex(GameError,'текстовый'):await resolve_text_channel(client,1,10)
        channel=Mock(spec=discord.TextChannel);channel.guild=SimpleNamespace(id=2)
        client.get_channel.return_value=channel
        with self.assertRaisesRegex(GameError,'этого сервера'):await resolve_text_channel(client,1,10)
