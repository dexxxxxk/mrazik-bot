import tempfile
import threading
import unittest
from pathlib import Path
from datetime import datetime,timezone
from types import SimpleNamespace as NS
from unittest.mock import patch,Mock,AsyncMock
import discord
from game import Game,GameError
from boss_engine import BossStore
from boss import BossService,BossButton

class BossTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'game.db')
        self.now=datetime(2026,9,15,9,tzinfo=timezone.utc).timestamp()
        self.game=Game(self.path,clock=lambda:self.now);self.game.configure(1,100,12,False)
        self.store=BossStore(self.game)
    def tearDown(self):self.game.db.close();self.tmp.cleanup()
    def start(self,hp=300):
        self.store.configure(1,True,12,hp);b=self.store.spawn(1)
        self.game.db.execute("UPDATE boss_runs SET message=200,state='open' WHERE id=?",(b['id'],))
        return self.store.get(b['id'])
    def hit(self,u,b):return self.store.hit(1,u,100,200,b['id'])
    def test_daily_schedule_once_and_no_catchup(self):
        self.now-=60;self.assertIsNone(self.store.spawn(1));self.now+=60
        a=self.store.spawn(1);self.assertEqual(a['id'],self.store.spawn(1,True)['id'])
        self.now+=86400*3;b=self.store.spawn(1)
        self.assertNotEqual(a['id'],b['id']);self.assertEqual(self.store.get(a['id'])['state'],'escaped')
        self.assertEqual(self.game.db.execute('SELECT count(*) FROM boss_runs').fetchone()[0],2)
    def test_hourly_per_player_and_restart(self):
        b=self.start(1500)
        with patch('boss_engine.random.randint',return_value=100):
            self.hit(10,b);self.hit(20,b)
            self.game.db.close();self.game=Game(self.path,clock=lambda:self.now);self.store=BossStore(self.game)
            self.assertEqual(self.store.get(b['id'])['hp'],1300)
            self.now+=3599
            with self.assertRaises(GameError):self.hit(10,b)
            self.now+=1;self.hit(10,b)
        self.assertEqual(self.store.members(b['id'])[0]['hits'],2)
        self.assertEqual(self.game.user(1,10)['coins'],0)
    def test_all_participants_paid_and_not_last_hit_winner(self):
        b=self.start()
        with patch('boss_engine.random.randint',return_value=140):
            self.hit(10,b);self.hit(20,b);damage,finished=self.hit(30,b)
        self.assertEqual(damage,20);self.assertEqual(finished['hp'],0)
        self.assertEqual(finished['state'],'defeated')
        self.assertEqual(self.game.user(1,10)['coins'],240)
        self.assertEqual(self.game.user(1,20)['coins'],240)
        self.assertEqual(self.game.user(1,30)['coins'],120)
        self.assertEqual(self.game.user(1,30)['xp'],72)
        with self.assertRaises(GameError):self.hit(40,b)
        self.assertEqual(self.store.spawn(1,True)['id'],b['id'])
        self.game.db.close();self.game=Game(self.path,clock=lambda:self.now);self.store=BossStore(self.game)
        self.assertEqual(self.game.user(1,10)['coins'],240)
        self.assertEqual(sum(m['damage'] for m in self.store.members(b['id'])),300)
    def test_failed_payout_rolls_back_entire_killing_hit(self):
        b=self.start()
        with patch('boss_engine.random.randint',return_value=100):
            self.hit(10,b);self.hit(20,b)
            real=self.game._reward;counter=[0]
            def fail(*a,**kw):
                counter[0]+=1
                if counter[0]==2:raise RuntimeError('simulate failure')
                return real(*a,**kw)
            with patch.object(self.game,'_reward',side_effect=fail):
                with self.assertRaises(RuntimeError):self.hit(30,b)
            self.assertEqual(self.store.get(b['id'])['hp'],100)
            self.assertEqual(len(self.store.members(b['id'])),2)
            self.assertEqual(self.game.user(1,10)['coins'],0)
            self.hit(30,b)
        self.assertEqual(self.game.user(1,10)['coins'],200)
    def test_two_simultaneous_killing_hits_pay_once(self):
        b=self.start()
        with patch('boss_engine.random.randint',return_value=100):self.hit(10,b);self.hit(20,b)
        barrier=threading.Barrier(2);out=[]
        def attack(uid):
            g=Game(self.path,clock=lambda:self.now);s=BossStore(g);barrier.wait()
            try:s.hit(1,uid,100,200,b['id']);out.append('hit')
            except GameError:out.append('closed')
            finally:g.db.close()
        with patch('boss_engine.random.randint',return_value=100):
            threads=[threading.Thread(target=attack,args=(n,)) for n in (30,40)]
            for t in threads:t.start()
            for t in threads:t.join(timeout=5)
        self.assertCountEqual(out,['hit','closed'])
        self.assertEqual(len(self.store.members(b['id'])),3)
        self.assertEqual(self.game.user(1,10)['coins'],200)
    def test_expiry_guild_message_channel_guards(self):
        b=self.start()
        for args in [(2,10,100,200),(1,10,101,200),(1,10,100,201)]:
            with self.assertRaises(GameError):self.store.hit(*args,b['id'])
        self.now=b['expires']
        with self.assertRaises(GameError):self.hit(10,b)
        self.store.expire();self.assertEqual(self.store.get(b['id'])['state'],'escaped')
    def test_settings_changes_do_not_reset_active_boss(self):
        b=self.start();self.store.configure(1,False,18,2000)
        self.assertEqual(self.store.spawn(1)['max_hp'],300)
        self.hit(10,b)
        self.now+=86400;self.assertIsNone(self.store.spawn(1))
        forced=self.store.spawn(1,True);self.assertEqual(forced['max_hp'],2000)
    def test_migration_backup_once_preserves_old_progress(self):
        self.game._reward(1,10,2000,6000,False);self.game.remember_role(1,0,999)
        self.game.db.execute("DELETE FROM migrations WHERE name='daily_boss_v5'")
        before=dict(self.game.user(1,10));BossStore(self.game);BossStore(self.game)
        self.assertEqual(dict(self.game.user(1,10)),before)
        self.assertEqual(self.game.rank_role_map(1)[0],999)
        self.assertEqual(len(list((Path(self.tmp.name)/'backups').glob('pre-daily-boss-*.sqlite3'))),1)

class BossUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.now=datetime(2026,9,15,9,tzinfo=timezone.utc).timestamp()
        self.game=Game(str(Path(self.tmp.name)/'game.db'),clock=lambda:self.now);self.game.configure(1,100,12,False)
        self.message=NS(id=200,edit=AsyncMock(),delete=AsyncMock())
        self.channel=Mock(spec=discord.TextChannel);self.channel.id=100;self.channel.guild=NS(id=1)
        self.channel.send=AsyncMock(return_value=self.message);self.channel.get_partial_message.return_value=self.message
        self.bot=NS(get_channel=Mock(return_value=self.channel))
        self.bot.features=NS(card=lambda t,d,a:{'embed':discord.Embed(title=t,description=d)},allowed=AsyncMock(return_value=True),respond_error=AsyncMock())
        self.service=BossService(self.bot,self.game);self.bot.boss=self.service
    async def asyncTearDown(self):self.game.db.close();self.tmp.cleanup()
    async def test_single_card_hit_and_offline_payout_then_cleanup(self):
        self.service.store.configure(1,True,12,300)
        b=await self.service.tick_guild(1);self.assertEqual(b['state'],'open')
        self.assertTrue(self.service.view(b).is_persistent())
        for uid in [10,20,30]:
            i=NS(client=self.bot,guild_id=1,channel_id=100,message=self.message,user=NS(id=uid),
                 response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))
            with patch('boss_engine.random.randint',return_value=100):await BossButton(b['id']).callback(i)
            self.bot.features.respond_error.assert_not_awaited()
            self.assertTrue(i.followup.send.call_args.kwargs['ephemeral'])
        self.channel.send.assert_awaited_once();self.assertEqual(self.message.edit.await_count,3)
        self.assertEqual(self.game.user(1,10)['coins'],200)
        self.now+=3601;await self.service.tick();self.message.delete.assert_awaited_once()
        self.assertEqual(self.service.store.get(b['id'])['cleaned'],1)
    async def test_failed_publish_retries_same_boss(self):
        self.channel.send.side_effect=RuntimeError('offline')
        b=await self.service.tick_guild(1);self.assertEqual(b['state'],'pending')
        await self.service.tick_guild(1);self.assertEqual(self.channel.send.await_count,1)
        self.now+=301;self.channel.send.side_effect=None
        posted=await self.service.tick_guild(1)
        self.assertEqual(posted['id'],b['id']);self.assertEqual(posted['state'],'open')
    async def test_channel_change_moves_same_boss(self):
        b=await self.service.tick_guild(1)
        self.game.configure(1,102,12,False)
        after=await self.service.tick_guild(1)
        self.message.delete.assert_awaited_once();self.assertEqual(after['channel'],102)
        self.assertEqual(after['hp'],b['hp']);self.assertEqual(after['id'],b['id'])
