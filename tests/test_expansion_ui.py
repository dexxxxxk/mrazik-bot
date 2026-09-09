import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,Mock
import discord
from game import Game
from features import FeatureService,EventButton
from expansion import Expansion,public_channels,NavButton

class ExpansionUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.game=Game(str(Path(self.tmp.name)/'test.db'))
        self.bot=NS()
        self.service=FeatureService(self.bot,self.game,lambda title,text,art:{'embed':discord.Embed(title=title,description=text)},AsyncMock(return_value=True),AsyncMock())
        self.bot.features=self.service;self.bot.expansion=Expansion(self.bot,self.game,lambda:discord.ui.View(timeout=None))
    async def asyncTearDown(self):self.game.db.close();self.tmp.cleanup()
    def interaction(self,private=True):
        return NS(guild_id=1,channel_id=100,user=NS(id=10),client=self.bot,
            message=NS(id=200,flags=NS(ephemeral=private)),response=NS(defer=AsyncMock()),
            edit_original_response=AsyncMock(return_value=NS(id=200)),followup=NS(send=AsyncMock(return_value=NS(id=201))))
    async def test_private_game_and_result_edit_same_message(self):
        i=self.interaction();await self.service.personal(i,'chests')
        i.followup.send.assert_not_awaited();i.edit_original_response.assert_awaited_once()
        e=self.game.event(self.game.db.execute('SELECT id FROM events').fetchone()[0])
        await EventButton(e['id'],0).callback(i)
        self.service.respond_error.assert_not_awaited()
        self.assertEqual(i.edit_original_response.await_count,2)
        i.followup.send.assert_not_awaited()
        self.assertTrue(any(isinstance(c,NavButton) for c in i.edit_original_response.call_args.kwargs['view'].children))
    async def test_drop_card_has_no_coins_key_and_collects_privately(self):
        e=self.game.create_loot_drop(1,100);self.game.open_event(e['id'],200)
        self.assertIn('инвентарь',self.service.event_card(e)['embed'].description)
        i=self.interaction(False);await EventButton(e['id'],0).callback(i)
        self.service.respond_error.assert_not_awaited();i.edit_original_response.assert_not_awaited()
        self.assertTrue(i.followup.send.call_args.kwargs['ephemeral']);self.assertEqual(len(self.game.inventory(1,10)),1)
    async def test_maze_navigation_binds_message_and_back_edits(self):
        i=self.interaction();await self.bot.expansion.navigate(i,'maze')
        m=self.game.maze(self.game.db.execute('SELECT id FROM maze_runs').fetchone()[0])
        self.assertEqual(m['message'],200)
        await self.bot.expansion.navigate(i,'home');i.followup.send.assert_not_awaited()
        self.assertEqual(i.edit_original_response.await_count,2)
    async def test_public_channels_respect_everyone_and_bot(self):
        guild=NS(me=object(),default_role=object())
        good=discord.Permissions(view_channel=True,send_messages=True,embed_links=True,attach_files=True,read_message_history=True)
        def channel(public,botperm):return NS(permissions_for=lambda who:discord.Permissions(view_channel=public) if who is guild.default_role else botperm)
        a=channel(True,good);b=channel(False,good);c=channel(True,discord.Permissions(view_channel=True))
        guild.text_channels=[a,b,c];self.assertEqual(public_channels(guild),[a])
    async def test_expired_public_message_deleted(self):
        e=self.game.create_loot_drop(1,100);self.game.open_event(e['id'],200)
        self.game.db.execute("UPDATE events SET state='closed' WHERE id=?",(e['id'],))
        channel=Mock(spec=discord.TextChannel);channel.guild=NS(id=1)
        message=NS(delete=AsyncMock());channel.get_partial_message.return_value=message
        self.bot.get_channel=Mock(return_value=channel);self.bot.expansion.tick_drops=AsyncMock()
        await self.service.tick();message.delete.assert_awaited_once()
        self.assertEqual(self.game.event(e['id'])['summary_done'],1)
