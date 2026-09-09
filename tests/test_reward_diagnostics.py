import tempfile
import unittest
from pathlib import Path
from features import FeatureService
from game import Game


class RewardDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.clock=1788958800.0
        self.game=Game(str(Path(self.tmp.name)/'db.sqlite3'),'Europe/Moscow',lambda:self.clock)
        self.service=FeatureService(None,self.game,None,None,None)
    def tearDown(self):self.game.db.close();self.tmp.cleanup()
    def cap(self):
        with self.game.tx():self.game._reward(1,10,300,200)

    def test_same_quiz_pays_both_members_after_old_cap(self):
        self.cap()
        e=self.game.start_activity(1,99,100,'quiz',True);self.game.open_event(e['id'],200)
        answer=e['payload']['answer']
        self.assertEqual(self.game.answer_activity(1,10,100,200,e['id'],answer)[0],(25,15))
        self.assertEqual(self.game.answer_activity(1,20,100,200,e['id'],answer)[0],(25,15))
        self.assertEqual(self.service.limit_notice(1,10,(25,15)),'')
        self.assertEqual(self.service.limit_notice(1,20,(25,15)),'')

    def test_approved_work_is_paid_above_cap_and_to_author(self):
        self.cap();sid=self.game.submit(1,10,'Моя работа','')
        self.assertEqual(self.game.user(1,10)['coins'],300)
        self.assertEqual(self.game.review(1,99,sid,True),(10,(80,40)))
        self.assertEqual((self.game.user(1,10)['coins'],self.game.pet(1,10)['xp']),(380,240))
        self.assertEqual(self.game.user(1,99)['coins'],0)
        self.assertEqual(self.service.daily_limits(1,10)['coins'],300)
        self.assertIn('одобрена',self.service.rewards_embed(1,10).fields[0].value)

    def test_pending_and_rejected_work_have_no_payment(self):
        sid=self.game.submit(1,10,'Проверьте','')
        self.assertIn('ожидает',self.service.rewards_embed(1,10).fields[0].value)
        self.assertEqual(self.game.review(1,99,sid,False),(10,(0,0)))
        self.assertIn('отклонена',self.service.rewards_embed(1,10).fields[0].value)
        self.assertEqual(self.game.user(1,10)['coins'],0)

    def test_diagnostics_are_read_only_and_reset_at_local_midnight(self):
        self.cap();before=self.game.db.total_changes
        current=self.service.daily_limits(1,10)
        self.service.rewards_embed(1,10);self.service.rewards_embed(1,88)
        self.assertEqual(before,self.game.db.total_changes)
        self.clock=current['reset']
        self.assertEqual(self.game.now().hour,0)
        limits=self.service.daily_limits(1,10)
        self.assertEqual((limits['coins_left'],limits['xp_left']),(None,None))

    def test_full_rewards_and_no_false_cap_notice(self):
        with self.game.tx():self.game._reward(1,10,300,90)
        with self.game.tx():reward=self.game._reward(1,10,25,15)
        self.assertEqual(reward,(25,15))
        notice=self.service.limit_notice(1,10,reward)
        self.assertEqual(notice,'')
