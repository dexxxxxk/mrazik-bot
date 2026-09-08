import tempfile
import threading
import unittest
from pathlib import Path
from game import Game, GameError


class V2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.tmp.name)/'game.db')
        self.time=1788868800.0
        self.game=Game(self.path,'UTC',lambda:self.time)

    def tearDown(self):
        self.game.db.close()
        self.tmp.cleanup()

    def public(self,kind='stash'):
        e=self.game.start_activity(1,10,100,kind,True)
        self.game.open_event(e['id'],200)
        return self.game.event(e['id'])

    def test_public_each_member_once_after_restart(self):
        e=self.public()
        self.assertEqual(self.game.answer_activity(1,10,100,200,e['id'],0)[0],(20,12))
        self.game.db.close(); self.game=Game(self.path,'UTC',lambda:self.time)
        with self.assertRaises(GameError): self.game.answer_activity(1,10,100,200,e['id'],0)
        self.assertEqual(self.game.answer_activity(1,11,100,200,e['id'],0)[0],(20,12))
        self.assertEqual(self.game.event_stats(e['id']),(2,2))

    def test_event_validates_origin_and_expiry(self):
        e=self.public()
        for g,ch,msg in [(2,100,200),(1,101,200),(1,100,201)]:
            with self.assertRaises(GameError): self.game.answer_activity(g,10,ch,msg,e['id'],0)
        self.time=e['expires']
        with self.assertRaises(GameError): self.game.answer_activity(1,10,100,200,e['id'],0)
        self.assertEqual(self.game.user(1,10)['coins'],0)

    def test_concurrent_public_clicks_pay_once(self):
        e=self.public(); barrier=threading.Barrier(2); results=[]
        def click():
            engine=Game(self.path,'UTC',lambda:self.time)
            try:
                barrier.wait()
                try: engine.answer_activity(1,10,100,200,e['id'],0); results.append('paid')
                except GameError: results.append('duplicate')
            finally: engine.db.close()
        workers=[threading.Thread(target=click) for _ in range(2)]
        for w in workers: w.start()
        for w in workers: w.join()
        self.assertCountEqual(results,['paid','duplicate'])
        self.assertEqual(self.game.user(1,10)['coins'],20)

    def test_wrong_answer_consumes_attempt(self):
        e=self.public('quiz'); answer=e['payload']['answer']
        r,correct,_=self.game.answer_activity(1,10,100,200,e['id'],(answer+1)%4)
        self.assertFalse(correct); self.assertEqual(r,(0,2))
        with self.assertRaises(GameError): self.game.answer_activity(1,10,100,200,e['id'],answer)

    def test_personal_owner_and_cooldown_survive_restart(self):
        e=self.game.start_activity(1,10,100,'chests'); self.game.open_event(e['id'],200)
        with self.assertRaises(GameError): self.game.answer_activity(1,11,100,200,e['id'],0)
        r,correct,_=self.game.answer_activity(1,10,100,200,e['id'],0)
        self.assertEqual(r,(e['payload']['amounts'][0],12)); self.assertTrue(correct)
        self.game.db.close(); self.game=Game(self.path,'UTC',lambda:self.time)
        with self.assertRaises(GameError): self.game.start_activity(1,10,100,'chests')
        with self.assertRaises(GameError): self.game.answer_activity(1,10,100,200,e['id'],0)

    def test_fish_timing_does_not_consume_early_attempt(self):
        e=self.game.start_activity(1,10,100,'fish'); self.game.open_event(e['id'],200)
        with self.assertRaises(GameError): self.game.answer_activity(1,10,100,200,e['id'],0)
        self.time=e['starts']
        self.assertTrue(self.game.answer_activity(1,10,100,200,e['id'],0)[1])

    def test_schedule_restart_and_downtime_do_not_duplicate(self):
        self.game.ensure_event_settings(1)
        self.game.db.execute('UPDATE event_settings SET next_at=?',(self.time-86400,))
        e=self.game.claim_scheduled_event(1,100); self.assertIsNotNone(e)
        self.game.db.close(); self.game=Game(self.path,'UTC',lambda:self.time)
        self.assertIsNone(self.game.claim_scheduled_event(1,100))
        self.assertGreater(self.game.ensure_event_settings(1)['next_at'],self.time)
        self.assertEqual(self.game.db.execute('SELECT count(*) FROM events').fetchone()[0],1)

    def test_quiet_hours_and_disable(self):
        hour=self.game.now().hour
        self.game.configure_events(1,True,hour,(hour+2)%24)
        self.game.db.execute('UPDATE event_settings SET next_at=?',(self.time-1,))
        self.assertIsNone(self.game.claim_scheduled_event(1,100))
        self.game.configure_events(1,True,0,0)
        e=self.public()
        self.game.configure_events(1,False)
        with self.assertRaises(GameError): self.game.answer_activity(1,10,100,200,e['id'],0)
        self.game.db.execute('UPDATE event_settings SET next_at=?',(self.time-1,))
        self.assertIsNone(self.game.claim_scheduled_event(1,100))
        self.assertTrue(self.game.in_quiet_hours({'quiet_start':23,'quiet_end':(hour+1)%24}))

    def test_new_games_share_reward_cap(self):
        with self.game.tx(): self.game._reward(1,10,299,199)
        e=self.public()
        self.assertEqual(self.game.answer_activity(1,10,100,200,e['id'],0)[0],(1,1))
        self.assertEqual(self.game.user(1,10)['coins'],300)

    def test_role_queue_retains_newer_xp_and_backfills(self):
        self.game.daily(1,10)
        self.game.role_ack(1,10,0)
        self.assertEqual(self.game.db.execute('SELECT xp FROM role_queue').fetchone()[0],20)
        self.game.role_ack(1,10,20)
        self.assertEqual(self.game.db.execute('SELECT count(*) FROM role_queue').fetchone()[0],0)
        self.game.enable_roles(1,True)
        self.assertEqual(self.game.db.execute('SELECT xp FROM role_queue').fetchone()[0],20)
        self.game.enable_roles(1,False)
        self.assertFalse(self.game.roles_enabled(1))

    def test_migration_preserves_old_data_and_old_settings_insert(self):
        self.game.daily(1,10)
        self.game.configure(1,100,12,True)
        for table in ['role_queue','role_settings','rank_roles','event_claims','events','event_settings']:
            self.game.db.execute('DROP TABLE '+table)
        self.game.db.close(); self.game=Game(self.path,'UTC',lambda:self.time)
        self.assertEqual(self.game.user(1,10)['coins'],60)
        self.assertEqual(self.game.settings(1)['channel'],100)
        self.game.configure(1,101,13,False)
        self.assertEqual(self.game.settings(1)['channel'],101)
        self.game.enable_roles(1,True)
        self.assertEqual(self.game.db.execute('SELECT xp FROM role_queue').fetchone()[0],20)
