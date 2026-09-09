import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from game import Game, GameError


class GameTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.temp.name)/'test.sqlite3')
        self.timestamp=datetime(2026,9,8,12,tzinfo=timezone.utc).timestamp()
        self.game=Game(self.path,'UTC',lambda:self.timestamp)

    def tearDown(self):
        self.game.db.close()
        self.temp.cleanup()

    def grant(self,coins=3000,xp=3000):
        with self.game.tx(): self.game._reward(1,10,coins,xp,False)

    def test_daily_is_paid_once_and_survives_restart(self):
        self.assertEqual(self.game.daily(1,10),((60,20),1))
        self.game.db.close()
        self.game=Game(self.path,'UTC',lambda:self.timestamp)
        with self.assertRaises(GameError): self.game.daily(1,10)
        self.assertEqual(self.game.user(1,10)['coins'],60)

    def test_streak_increases_then_resets_after_missed_day(self):
        self.game.daily(1,10)
        self.timestamp+=86400
        self.assertEqual(self.game.daily(1,10),((70,20),2))
        self.timestamp+=2*86400
        self.assertEqual(self.game.daily(1,10),((60,20),1))

    def test_daily_boundary_uses_selected_timezone(self):
        self.game.tz=__import__('zoneinfo').ZoneInfo('Europe/Moscow')
        self.timestamp=datetime(2026,9,8,20,59,tzinfo=timezone.utc).timestamp()
        self.game.daily(1,10)
        self.timestamp+=120
        self.assertEqual(self.game.daily(1,10)[1],2)

    def test_duplicate_purchase_does_not_charge_twice(self):
        self.grant()
        self.game.purchase(1,10,'king')
        with self.assertRaises(GameError): self.game.purchase(1,10,'king')
        self.assertEqual(self.game.user(1,10)['coins'],1500)
        self.assertEqual(self.game.owned(1,10).count('king'),1)

    def test_rank_and_funds_fail_without_mutation(self):
        self.grant(3000,0)
        with self.assertRaises(GameError): self.game.purchase(1,10,'king')
        self.assertEqual(self.game.user(1,10)['coins'],3000)
        with self.assertRaises(GameError): self.game.purchase(1,11,'hobo')
        self.assertEqual(self.game.user(1,11)['coins'],0)

    def test_unowned_outfit_cannot_be_equipped(self):
        with self.assertRaises(GameError): self.game.equip(1,10,'king')
        with self.assertRaises(GameError): self.game.equip(1,10,'king',True)

    def test_equipping_changes_only_owners_pet_without_shared_lock(self):
        self.grant()
        self.game.purchase(1,10,'hobo')
        self.game.equip(1,10,'hobo',True)
        self.assertEqual(self.game.pet(1,10)['outfit'],'hobo')
        self.assertEqual(self.game.pet(1,11)['outfit'],'base')
        self.game.equip(1,10,'base')
        self.assertEqual(self.game.pet(1,10)['outfit'],'base')

    def test_game_rewards_continue_beyond_old_daily_cap(self):
        for _ in range(30): self.game.quiz_reward(1,10,True)
        u=self.game.user(1,10)
        self.assertEqual((u['coins'],u['xp']),(750,450))
        self.game.daily(1,10)
        u=self.game.user(1,10)
        self.assertEqual((u['coins'],u['xp']),(810,470))
        self.timestamp+=86400
        self.assertEqual(self.game.quiz_reward(1,10,True),(25,15))

    def test_failed_care_rolls_back_cooldowns(self):
        self.game.pet(1,10)
        self.game.db.execute('UPDATE personal_pets SET food=100 WHERE guild=1 AND uid=10')
        with self.assertRaises(GameError): self.game.care(1,10,'feed')
        self.assertEqual(self.game.care(1,10,'play'),(10,10))
        with self.assertRaises(GameError): self.game.care(1,10,'play')

    def test_personal_care_does_not_block_other_users(self):
        self.game.care(1,10,'play')
        self.assertEqual(self.game.care(1,11,'play'),(10,10))
        with self.assertRaises(GameError):self.game.care(1,10,'play')

    def test_pet_never_dies_and_stats_are_bounded_after_long_absence(self):
        self.game.pet(1,10)
        self.timestamp+=365*86400
        p=self.game.pet(1,10)
        self.assertEqual((p['food'],p['mood'],p['energy']),(0,0,100))

    def test_servers_have_separate_balances_pets_and_cooldowns(self):
        self.game.daily(1,10)
        self.assertEqual(self.game.user(2,10)['coins'],0)
        self.game.daily(2,10)
        self.game.cooldown(1,10,'quiz',60)
        self.game.cooldown(2,10,'quiz',60)
        self.game.care(1,10,'feed')
        self.assertEqual(self.game.pet(2,10)['xp'],20)
        self.assertEqual(self.game.pet(2,11)['xp'],0)

    def test_submissions_are_once_per_day_and_review_once(self):
        sid=self.game.submit(1,10,'Мем','')
        with self.assertRaises(GameError): self.game.submit(1,10,'Ещё мем','')
        self.assertEqual(self.game.review(1,99,sid,True),(10,(80,40)))
        with self.assertRaises(GameError): self.game.review(1,99,sid,True)
        self.assertEqual(self.game.user(1,10)['coins'],80)

    def test_review_cannot_cross_servers_or_approve_own_work(self):
        sid=self.game.submit(1,10,'Мем','')
        with self.assertRaises(GameError): self.game.review(2,99,sid,True)
        with self.assertRaises(GameError): self.game.review(1,10,sid,True)
        self.assertEqual(self.game.review(1,99,sid,False),(10,(0,0)))

    def test_title_only_draws_from_opted_in_users_and_exit_removes_choice(self):
        self.game.user(1,99)
        with self.assertRaises(GameError): self.game.title(1)
        self.game.opt(1,10,True)
        first=self.game.title(1)
        self.assertEqual(first[0],10)
        self.assertEqual(self.game.title(1),first)
        self.game.opt(1,10,False)
        with self.assertRaises(GameError): self.game.title(1)

    def test_weekly_board_resets_without_losing_all_time_progress(self):
        self.game.daily(1,10)
        self.assertEqual(self.game.top(1)[0][1],20)
        self.timestamp+=7*86400
        self.assertEqual(self.game.top(1),[])
        self.assertEqual(self.game.top(1,False)[0][1],20)

    def test_expedition_cooldown_survives_restart(self):
        self.game.expedition(1,10)
        self.game.db.close()
        self.game=Game(self.path,'UTC',lambda:self.timestamp)
        with self.assertRaises(GameError): self.game.expedition(1,10)

    def test_auto_post_claim_is_unique(self):
        self.assertTrue(self.game.claim_post(1))
        self.assertFalse(self.game.claim_post(1))
        self.assertTrue(self.game.claim_post(2))

    def test_concurrent_daily_requests_pay_once(self):
        outcomes=[]
        barrier=threading.Barrier(2)
        def request():
            g=Game(self.path,'UTC',lambda:self.timestamp)
            try:
                barrier.wait()
                g.daily(1,10)
                outcomes.append('paid')
            except GameError: outcomes.append('blocked')
            finally: g.db.close()
        threads=[threading.Thread(target=request) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10)
        self.assertCountEqual(outcomes,['paid','blocked'])
        self.assertEqual(self.game.user(1,10)['coins'],60)


if __name__=='__main__': unittest.main()
