import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,Mock,patch
import discord
from game import Game,GameError
from roaming import Roaming,channel_reason
from features import FeatureService,EventButton

class RoamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.now=1788940000;self.path=str(Path(self.tmp.name)/'game.db')
        self.game=Game(self.path,clock=lambda:self.now);self.game.configure(1,100,12,False)
        self.guild=NS(id=1,me=object(),default_role=object())
        self.channels=[]
        for n in [101,102]:
            c=Mock(spec=discord.TextChannel);c.id=n;c.name=f'open{n}'
            c.permissions_for.return_value=discord.Permissions(view_channel=True,send_messages=True,embed_links=True,attach_files=True)
            self.channels.append(c)
        self.guild.fetch_channels=AsyncMock(return_value=self.channels)
        self.bot=NS(user=NS(id=9))
        self.service=FeatureService(self.bot,self.game,lambda t,d,a:{'embed':discord.Embed(title=t,description=d)},AsyncMock(return_value=True),AsyncMock())
        self.bot.features=self.service
        async def publish(channel,e):self.game.open_event(e['id'],200);return NS(id=200)
        self.service.publish=AsyncMock(side_effect=publish)
        self.roaming=Roaming(self.bot,self.game)
    async def asyncTearDown(self):self.game.db.close();self.tmp.cleanup()
    async def test_enable_sends_after_minute_rotates_and_persists_schedule(self):
        self.roaming.configure(1,True,60,120)
        await self.roaming.send(self.guild);self.service.publish.assert_not_awaited()
        self.now+=61;await self.roaming.send(self.guild)
        first=self.roaming.state(1);self.assertEqual(first['last_success'],self.now)
        self.assertTrue(self.now+3600<=first['next_at']<=self.now+7200)
        await self.roaming.send(self.guild);self.assertEqual(self.service.publish.await_count,1)
        self.now=first['next_at'];await self.roaming.send(self.guild)
        second=self.roaming.state(1)
        self.assertNotEqual(first['last_channel'],second['last_channel']);self.assertNotEqual(first['last_kind'],second['last_kind'])
        self.game.db.close();self.game=Game(self.path,clock=lambda:self.now);self.roaming=Roaming(self.bot,self.game)
        self.assertEqual(self.roaming.state(1)['next_at'],second['next_at'])
    async def test_no_channels_and_send_failure_visible_and_retry(self):
        self.roaming.configure(1,True,60,120);self.guild.fetch_channels.return_value=[]
        with self.assertRaises(GameError):await self.roaming.send(self.guild,True)
        self.assertIn('Нет открытых',self.roaming.state(1)['error'])
        self.assertEqual(self.roaming.state(1)['next_at'],self.now+300)
        self.guild.fetch_channels.return_value=self.channels;self.now+=301
        self.service.publish.side_effect=RuntimeError('send failed')
        await self.roaming.send(self.guild)
        self.assertIn('send failed',self.roaming.state(1)['error'])
        self.assertEqual(self.roaming.state(1)['next_at'],self.now+300)
    async def test_private_channel_excluded_read_history_not_required(self):
        c=self.channels[0];self.assertEqual(channel_reason(c,self.guild),'')
        c.permissions_for.side_effect=lambda who:discord.Permissions.none() if who is self.guild.default_role else discord.Permissions.all()
        self.assertIn('закрыт',channel_reason(c,self.guild))
    async def test_all_variants_cards_and_one_reward_outside_game_channel(self):
        for variant in ['drop','rescue','parcel','caravan']:
            e=self.roaming.create_event(1,102,variant);self.game.open_event(e['id'],200)
            self.assertTrue(self.service.event_card(e)['embed'].title)
            if variant=='drop':
                cid=self.game.collect_drop(1,10,102,200,e['id']);self.assertTrue(cid)
                continue
            choice=e['payload'].get('answer',0)
            i=NS(client=self.bot,guild_id=1,channel_id=102,user=NS(id=10),message=NS(id=200),
                 response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))
            await EventButton(e['id'],choice).callback(i)
            self.service.respond_error.assert_not_awaited()
            self.service.allowed.assert_awaited_with(i,True)
            self.assertTrue(i.followup.send.call_args.kwargs['ephemeral'])
            with self.assertRaises(GameError):self.game.answer_activity(1,10,102,200,e['id'],choice)
    async def test_disabled_and_active_do_not_duplicate(self):
        self.roaming.configure(1,False,60,120);await self.roaming.send(self.guild,True)
        self.service.publish.assert_not_awaited()
        self.roaming.configure(1,True,60,120);await self.roaming.send(self.guild,True)
        with self.assertRaises(GameError):await self.roaming.send(self.guild,True)
        self.now+=121;answer=await self.roaming.send(self.guild,True)
        self.assertIn('Уже идёт',answer);self.assertEqual(self.service.publish.await_count,1)
    async def test_migration_preserves_progress_and_disabled_setting(self):
        self.game._reward(1,10,700,500)
        self.roaming.configure(1,False,30,90)
        Roaming(self.bot,self.game)
        self.assertEqual(self.game.user(1,10)['coins'],700)
        self.assertFalse(self.roaming.state(1)['enabled'])
        self.assertEqual(self.roaming.state(1)['min_minutes'],30)
