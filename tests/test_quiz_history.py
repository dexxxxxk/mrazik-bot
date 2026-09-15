import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from content import QUIZ
from game import Game,GameError
from quiz_history import question_id

BANK=[('Первый вопрос?',['А','Б','В','Г'],'А'),('Второй вопрос?',['А','Б','В','Г'],'Б')]


class QuizHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'game.db')
        self.time=1788868800.0;self.game=Game(self.path,'UTC',lambda:self.time)
    def tearDown(self):
        self.game.db.close();self.tmp.cleanup()
    def restart(self):
        self.game.db.close();self.game=Game(self.path,'UTC',lambda:self.time)
    def personal(self,u=10):
        e=self.game.start_activity(1,u,100,'quiz');self.game.open_event(e['id'],200)
        return self.game.event(e['id'])

    def test_opened_unanswered_question_never_repeats_after_restart(self):
        with patch('quiz_history.QUIZ',BANK):
            a=self.personal();self.time+=601;self.restart();b=self.personal()
            self.assertNotEqual(a['payload']['qid'],b['payload']['qid'])
            self.time+=601
            with self.assertRaisesRegex(GameError,'закончились'):self.personal()
            self.assertEqual(self.game.user(1,10)['xp'],0)

    def test_daily_limit_and_cooldown_persist(self):
        self.personal()
        with self.assertRaises(GameError):self.personal()
        self.time+=601;self.personal();self.time+=601;self.personal();self.time+=601;self.restart()
        with self.assertRaisesRegex(GameError,'Три личных'):self.personal()
        self.time+=86400;self.personal()

    def test_personal_answer_has_durable_deadline_and_one_reward(self):
        e=self.personal();answer=e['payload']['answer'];self.restart()
        self.assertEqual(self.game.answer_activity(1,10,100,200,e['id'],answer)[0],(25,15))
        with self.assertRaises(GameError):self.game.answer_activity(1,10,100,200,e['id'],answer)
        other=self.personal(11);self.time=other['expires']
        with self.assertRaises(GameError):self.game.answer_activity(1,11,100,200,other['id'],other['payload']['answer'])

    def test_personal_question_cannot_be_farmed_in_public(self):
        with patch('quiz_history.QUIZ',BANK[:1]):
            personal=self.personal()
            e=self.game.start_activity(1,99,100,'quiz',True);self.game.open_event(e['id'],300)
            with self.assertRaisesRegex(GameError,'уже попадался'):
                self.game.answer_activity(1,10,100,300,e['id'],e['payload']['answer'])
            self.assertEqual(self.game.answer_activity(1,11,100,300,e['id'],e['payload']['answer'])[0],(25,15))

    def test_public_question_not_later_given_personally(self):
        with patch('quiz_history.QUIZ',BANK):
            e=self.game.start_activity(1,99,100,'quiz',True)
            p=self.personal()
            self.assertNotEqual(e['payload']['qid'],p['payload']['qid'])

    def test_public_bank_exhaustion_keeps_other_hourly_events(self):
        with patch('quiz_history.QUIZ',BANK[:1]):
            self.game.start_activity(1,99,100,'quiz',True);self.time+=601
            with self.assertRaisesRegex(GameError,'закончились'):self.game.start_activity(1,99,100,'quiz',True)
            self.game.ensure_event_settings(1)
            self.game.db.execute('UPDATE event_settings SET next_at=?',(self.time-1,))
            e=self.game.claim_scheduled_event(1,100)
            self.assertIn(e['kind'],['stash','target'])

    def test_existing_v2_public_history_is_imported(self):
        q=QUIZ[0]
        self.game.db.execute("DELETE FROM migrations WHERE name='quiz_history_2_1'")
        self.game.db.execute("INSERT INTO events(id,guild,channel,kind,payload,starts,expires) VALUES('legacy',1,100,'quiz',?,0,1)",
            (json.dumps(dict(text=q[0],options=q[1],answer=0)),))
        self.restart()
        self.assertNotIn(q,self.game.available_questions(1,10))
        self.restart()
        self.assertEqual(self.game.db.execute('SELECT count(*) FROM quiz_public_seen').fetchone()[0],1)

    def test_reordering_preserves_history_adding_question_extends_bank(self):
        with patch('quiz_history.QUIZ',BANK[:1]):a=self.personal()
        self.time+=601
        with patch('quiz_history.QUIZ',list(reversed(BANK))):b=self.personal()
        self.assertNotEqual(a['payload']['qid'],b['payload']['qid'])

    def test_simultaneous_personal_open_reserves_only_one(self):
        barrier=threading.Barrier(2);results=[]
        def open_quiz():
            game=Game(self.path,'UTC',lambda:self.time)
            try:
                barrier.wait()
                try:game.start_activity(1,10,100,'quiz');results.append('opened')
                except GameError:results.append('blocked')
            finally:game.db.close()
        threads=[threading.Thread(target=open_quiz) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertCountEqual(results,['opened','blocked'])
        self.assertEqual(self.game.db.execute('SELECT count FROM quiz_daily').fetchone()[0],1)
        self.assertEqual(self.game.db.execute('SELECT count(*) FROM quiz_seen').fetchone()[0],1)

    def test_question_bank_has_unique_questions_and_valid_discord_options(self):
        self.assertGreaterEqual(len(QUIZ),60)
        self.assertEqual(len({question_id(q[0]) for q in QUIZ}),len(QUIZ))
        for text,options,answer in QUIZ:
            self.assertEqual(len(options),4);self.assertEqual(len(set(options)),4)
            self.assertEqual(options.count(answer),1)
            self.assertTrue(all(1<=len(option)<=80 for option in options))
