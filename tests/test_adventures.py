import tempfile
import unittest
from pathlib import Path
from collections import deque
from unittest.mock import patch
from game import Game,GameError
from collection import RARE_OUTFITS

class AdventuresTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'game.db')
        self.now=1788940000;self.game=Game(self.path,clock=lambda:self.now)
    def tearDown(self):self.game.db.close();self.tmp.cleanup()
    def chest(self,u=10):
        e=self.game.create_loot_drop(1,100);self.game.open_event(e['id'],200)
        return self.game.collect_drop(1,u,100,200,e['id']),e
    def test_claim_binding_once_and_multiple_players(self):
        cid,e=self.chest()
        for g,u,ch,msg in [(1,10,100,200),(2,10,100,200),(1,20,101,200),(1,20,100,201)]:
            with self.assertRaises(GameError):self.game.collect_drop(g,u,ch,msg,e['id'])
        self.game.collect_drop(1,20,100,200,e['id'])
        self.assertEqual(len(self.game.inventory(1,10)),1)
        self.assertEqual(len(self.game.inventory(1,20)),1)
        self.now+=300
        with self.assertRaises(GameError):self.game.collect_drop(1,30,100,200,e['id'])
    def test_open_once_uncapped_and_owner_bound(self):
        cid,_=self.chest();self.game._reward(1,10,300,200)
        with self.assertRaises(GameError):self.game.open_loot(1,20,cid)
        with self.assertRaises(GameError):self.game.open_loot(2,10,cid)
        with patch('adventures.random.random',return_value=.8):r=self.game.open_loot(1,10,cid)
        self.assertGreaterEqual(r['coins'],50);self.assertGreaterEqual(r['xp'],15)
        self.assertEqual(self.game.user(1,10)['coins'],300+r['coins'])
        with self.assertRaises(GameError):self.game.open_loot(1,10,cid)
        self.assertEqual(self.game.inventory(1,10),[])
    def test_rare_duplicate_and_not_purchasable(self):
        cid,_=self.chest()
        with patch('adventures.random.random',return_value=.1),patch('adventures.random.choice',return_value='dragon'):
            self.assertEqual(self.game.open_loot(1,10,cid)['kind'],'outfit')
            cid,_=self.chest();r=self.game.open_loot(1,10,cid)
        self.assertEqual((r['kind'],r['coins'],r['xp']),('duplicate',150,40))
        self.assertIn('dragon',self.game.owned(1,10));self.assertEqual(self.game.user(1,10)['outfit'],'base')
        for key in RARE_OUTFITS:
            with self.assertRaises(GameError):self.game.purchase(1,20,key)
    def test_inventory_and_progress_survive_restart(self):
        cid,_=self.chest();self.game._reward(1,10,200,100)
        self.game.remember_role(1,0,999)
        snapshot=dict(self.game.user(1,10));self.game.db.close();self.game=Game(self.path,clock=lambda:self.now)
        self.assertEqual(dict(self.game.user(1,10)),snapshot)
        self.assertEqual(self.game.rank_role_map(1)[0],999)
        self.assertEqual(self.game.inventory(1,10),[{'id':cid}])
    def test_schedule_no_catchup_flood_and_toggle(self):
        self.assertFalse(self.game.claim_loot_slot(1))
        self.game.db.execute('UPDATE loot_schedule SET next_at=0')
        self.assertTrue(self.game.claim_loot_slot(1));self.assertFalse(self.game.claim_loot_slot(1))
        self.game.db.execute('UPDATE loot_schedule SET enabled=0,next_at=0')
        self.assertFalse(self.game.claim_loot_slot(1))
    def test_hourly_reward_ignores_cap_manual_keeps_cap(self):
        self.game._reward(1,10,300,200)
        self.game.configure_events(1,True,0,0);self.game.db.execute('UPDATE event_settings SET next_at=0')
        with patch('activities.random.choice',side_effect=lambda xs:'stash' if 'stash' in xs else xs[0]):e=self.game.claim_scheduled_event(1,100)
        self.game.open_event(e['id'],200)
        r,correct,_=self.game.answer_activity(1,10,100,200,e['id'],e['payload']['answer'])
        self.assertTrue(correct);self.assertEqual(r,(e['payload']['coins'],e['payload']['xp']))
        self.now+=301;self.game.expire_events()
        manual=self.game.start_activity(1,10,100,'stash',True);self.game.open_event(manual['id'],201)
        r,_,_=self.game.answer_activity(1,10,100,201,manual['id'],0);self.assertEqual(r,(0,0))
    def test_maze_solvable_persistent_and_pays_once(self):
        m=self.game.start_maze(1,10,100);self.game.db.execute('UPDATE maze_runs SET message=200 WHERE id=?',(m['id'],))
        self.assertEqual(self.game.start_maze(1,10,100)['id'],m['id'])
        other=self.game.start_maze(1,20,100);self.assertNotEqual(other['id'],m['id'])
        with self.assertRaises(GameError):self.game.move_maze(1,20,100,200,m['id'],'e')
        self.game.db.close();self.game=Game(self.path,clock=lambda:self.now)
        q=deque([((1,1),[])]);seen={(1,1)};route=None
        while q:
            (x,y),path=q.popleft()
            if (x,y)==(7,7):route=path;break
            for d,dx,dy in [('n',0,-1),('s',0,1),('w',-1,0),('e',1,0)]:
                p=(x+dx,y+dy)
                if p not in seen and not m['grid'][p[1]][p[0]]:seen.add(p);q.append((p,path+[d]))
        self.assertIsNotNone(route)
        for d in route:m,reward=self.game.move_maze(1,10,100,200,m['id'],d)
        self.assertTrue(m['done']);self.assertEqual(reward,(40,25))
        with self.assertRaises(GameError):self.game.move_maze(1,10,100,200,m['id'],'w')
    def test_maze_expiration_and_message_binding(self):
        m=self.game.start_maze(1,10,100)
        with self.assertRaises(GameError):self.game.move_maze(1,10,100,999,m['id'],'e')
        self.now+=600
        with self.assertRaises(GameError):self.game.move_maze(1,10,100,0,m['id'],'e')
        self.assertNotEqual(self.game.start_maze(1,10,100)['id'],m['id'])

    def test_upgrade_adds_tables_once_and_backs_up_existing_data(self):
        self.game._reward(1,10,150,75)
        self.game.remember_role(1,0,999)
        self.game.db.execute("DELETE FROM migrations WHERE name='adventures_v4'")
        for table in ['loot_schedule','inventory_chests','maze_runs']:self.game.db.execute('DROP TABLE '+table)
        before=dict(self.game.user(1,10));self.game.db.close()
        self.game=Game(self.path,clock=lambda:self.now)
        backups=list((Path(self.tmp.name)/'backups').glob('pre-adventures-*.sqlite3'))
        self.assertEqual(len(backups),1)
        self.assertEqual(dict(self.game.user(1,10)),before)
        self.assertEqual(self.game.rank_role_map(1)[0],999)
        self.game.db.close();self.game=Game(self.path,clock=lambda:self.now)
        self.assertEqual(len(list((Path(self.tmp.name)/'backups').glob('pre-adventures-*.sqlite3'))),1)
        self.assertEqual(dict(self.game.user(1,10)),before)
