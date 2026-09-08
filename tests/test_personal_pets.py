import sqlite3
import tempfile
import unittest
from pathlib import Path
from game import Game
from content import RANKS


class PersonalPetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'game.db')
        self.time=1788868800.0;self.game=Game(self.path,'UTC',lambda:self.time)
    def tearDown(self):self.game.db.close();self.tmp.cleanup()
    def restart(self):
        self.game.db.close();self.game=Game(self.path,'UTC',lambda:self.time)

    def test_care_changes_only_one_pet_and_role_xp(self):
        before=self.game.pet(1,20)
        self.game.care(1,10,'feed')
        self.assertEqual(self.game.pet(1,20),before)
        p=self.game.pet(1,10)
        self.assertEqual((p['food'],p['xp']),(90,10))
        queued=self.game.db.execute('SELECT guild,uid,xp FROM role_queue').fetchone()
        self.assertEqual(tuple(queued),(1,10,10))

    def test_migration_preserves_old_progress_and_role_ids_and_makes_backup(self):
        with self.game.tx():self.game._reward(1,10,1000,450,False)
        self.game.purchase(1,10,'mage');self.game.equip(1,10,'mage')
        self.game.configure(1,999,12,True)
        for n,(xp,_) in enumerate(RANKS):self.game.remember_role(1,xp,700+n)
        self.game.enable_roles(1,True)
        self.game.db.execute('INSERT INTO pets(guild,food,mood,energy,updated,xp,outfit) VALUES(1,55,44,66,?,9000,?)',(self.time,'king'))
        expected_user=self.game.user(1,10);expected_roles=self.game.rank_role_map(1)
        self.game.db.execute('DROP TABLE personal_pets')
        self.game.db.execute("DELETE FROM migrations WHERE name='personal_pets_v3'")
        self.restart()
        p=self.game.pet(1,10)
        self.assertEqual((p['food'],p['mood'],p['energy'],p['xp'],p['outfit']),(55,44,66,450,'mage'))
        self.assertEqual(self.game.user(1,10),expected_user)
        self.assertEqual(self.game.rank_role_map(1),expected_roles)
        self.assertEqual(self.game.settings(1)['channel'],999)
        self.assertTrue(self.game.roles_enabled(1))
        backups=list((Path(self.path).parent/'backups').glob('*.sqlite3'));self.assertEqual(len(backups),1)
        db=sqlite3.connect(backups[0])
        try:self.assertEqual(db.execute('SELECT xp,outfit FROM users WHERE uid=10').fetchone(),(450,'mage'))
        finally:db.close()
        # Subsequent startups must never clone the old common state again.
        self.game.care(1,10,'feed');self.game.equip(1,10,'base')
        before=self.game.pet(1,10);self.restart();self.restart()
        self.assertEqual(self.game.pet(1,10),before)
        self.assertEqual(len(list((Path(self.path).parent/'backups').glob('*.sqlite3'))),1)
        self.assertEqual(self.game.rank_role_map(1),expected_roles)
        self.assertEqual(self.game.db.execute('SELECT xp,outfit FROM pets WHERE guild=1').fetchone()[:],(9000,'king'))

    def test_new_members_start_independently(self):
        self.game.care(1,10,'play')
        self.time+=86400
        p=self.game.pet(1,99)
        self.assertEqual((p['food'],p['mood'],p['energy'],p['xp'],p['outfit']),(70,70,70,0,'base'))
        self.assertEqual(self.game.pet(1,10)['food'],0)

    def test_rewards_from_games_and_daily_are_pet_experience(self):
        self.game.daily(1,10)
        self.assertEqual(self.game.pet(1,10)['xp'],20)
        e=self.game.start_activity(1,10,100,'chests');self.game.open_event(e['id'],200)
        self.game.answer_activity(1,10,100,200,e['id'],0)
        self.assertEqual(self.game.pet(1,10)['xp'],32)
        self.assertEqual(self.game.pet(1,20)['xp'],0)
