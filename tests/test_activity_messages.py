import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import discord
from features import FeatureService
from game import Game,GameError


def card(title,description,art):
    return {'embed':discord.Embed(title=title,description=description)}


class ActivityMessageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.game=Game(str(Path(self.tmp.name)/'game.db'))
        self.service=FeatureService(None,self.game,card,None,None)
    async def asyncTearDown(self):self.game.db.close();self.tmp.cleanup()

    async def test_all_game_cards_accept_actual_engine_payload(self):
        for guild,(kind,public) in enumerate([('chests',False),('guess',False),('fish',False),('quiz',False),('quiz',True),('stash',True),('target',True)],1):
            with self.subTest(kind=kind,public=public):
                e=self.game.start_activity(guild,10,100,kind,public)
                kwargs=self.service.event_card(e)
                self.assertTrue(kwargs['embed'].title)
                self.assertLess(len(kwargs['embed'].description),4096)
                self.assertEqual(len(self.service.event_view(e).children),len(e['payload']['options'])+1)
                if kind=='chests':
                    self.assertNotIn('coins',e['payload'])
                    self.assertIn('10, 30, 60',kwargs['embed'].description)
                if kind=='quiz':
                    hint='Без повторов' if not public else 'Если ты уже открывал'
                    self.assertEqual(kwargs['embed'].description.count(hint),1)

    async def test_personal_chests_publish_then_pay_once(self):
        interaction=SimpleNamespace(guild_id=1,user=SimpleNamespace(id=10),channel_id=100,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=200))))
        await self.service.personal(interaction,'chests')
        interaction.followup.send.assert_awaited_once()
        args=interaction.followup.send.call_args.kwargs
        self.assertTrue(args['ephemeral']);self.assertIn('10, 30, 60',args['embed'].description)
        e=self.game.event(self.game.db.execute('SELECT id FROM events').fetchone()[0])
        self.assertEqual((e['state'],e['message']),('open',200))
        reward,correct,_=self.game.answer_activity(1,10,100,200,e['id'],1)
        self.assertEqual(reward,(e['payload']['amounts'][1],12));self.assertTrue(correct)
        with self.assertRaises(GameError):self.game.answer_activity(1,10,100,200,e['id'],2)
