"""Durable collectible chests and personal button mazes."""
import json
import random
import uuid
import sqlite3
from pathlib import Path
from collection import RARE_OUTFITS


class AdventuresMixin:
    def init_adventures(self):
        migrated=self.db.execute("SELECT 1 FROM migrations WHERE name='adventures_v4'").fetchone()
        if not migrated and self.db.execute('SELECT 1 FROM users LIMIT 1').fetchone():
            dbfile=self.db.execute('PRAGMA database_list').fetchone()[2]
            if dbfile:
                folder=Path(dbfile).parent/'backups';folder.mkdir(parents=True,exist_ok=True)
                dest=sqlite3.connect(folder/f'pre-adventures-{uuid.uuid4().hex}.sqlite3')
                try:self.db.backup(dest)
                finally:dest.close()
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS loot_schedule(guild INTEGER PRIMARY KEY,enabled INTEGER DEFAULT 1,next_at REAL);
        CREATE TABLE IF NOT EXISTS inventory_chests(id TEXT PRIMARY KEY,guild INTEGER,uid INTEGER,event TEXT,
            opened INTEGER DEFAULT 0,result TEXT DEFAULT '',UNIQUE(event,uid));
        CREATE TABLE IF NOT EXISTS maze_runs(id TEXT PRIMARY KEY,guild INTEGER,uid INTEGER,grid TEXT,
            x INTEGER,y INTEGER,moves INTEGER DEFAULT 0,expires REAL,done INTEGER DEFAULT 0,
            message INTEGER DEFAULT 0,channel INTEGER DEFAULT 0);
        ''')
        self.db.execute("INSERT OR IGNORE INTO migrations VALUES('adventures_v4')")

    def loot_settings(self,g):
        self.db.execute('INSERT OR IGNORE INTO loot_schedule(guild,next_at) VALUES(?,?)',(g,self.clock()+random.randint(7200,14400)))
        return dict(self.db.execute('SELECT * FROM loot_schedule WHERE guild=?',(g,)).fetchone())

    def claim_loot_slot(self,g):
        with self.tx():
            cfg=self.loot_settings(g)
            if not cfg['enabled'] or cfg['next_at']>self.clock():return False
            self.db.execute('UPDATE loot_schedule SET next_at=? WHERE guild=?',(self.clock()+random.randint(7200,14400),g))
            return True

    def create_loot_drop(self,g,channel):
        with self.tx():
            eid=uuid.uuid4().hex
            payload=dict(title='Мразик обронил коллекционный сундук!',text='Забери в инвентарь. Каждый участник может получить один сундук за это событие.',
                options=['🎁 Забрать сундук'],art='pharaoh',uncapped=True)
            self.db.execute('INSERT INTO events(id,guild,channel,kind,payload,starts,expires) VALUES(?,?,?,?,?,?,?)',
                (eid,g,channel,'drop',json.dumps(payload,ensure_ascii=False),self.clock(),self.clock()+300))
            return self.event(eid)

    def collect_drop(self,g,u,channel,message,eid):
        from activities import GameError
        with self.tx():
            e=self.event(eid)
            if (e['guild'],e['channel'],e['message'],e['kind'])!=(g,channel,message,'drop'):raise GameError('Чужое событие.')
            if e['state']!='open' or self.clock()>=e['expires']:raise GameError('Событие закончилось.')
            if self.db.execute('SELECT 1 FROM inventory_chests WHERE event=? AND uid=?',(eid,u)).fetchone():raise GameError('Ты уже забрал этот сундук. Он в /инвентарь.')
            self.user(g,u)
            cid=uuid.uuid4().hex
            self.db.execute('INSERT INTO inventory_chests(id,guild,uid,event) VALUES(?,?,?,?)',(cid,g,u,eid))
            self.db.execute('INSERT INTO event_claims VALUES(?,?,1,0,0)',(eid,u))
            return cid

    def inventory(self,g,u):
        return [dict(r) for r in self.db.execute('SELECT id FROM inventory_chests WHERE guild=? AND uid=? AND opened=0 ORDER BY rowid',(g,u))]

    def open_loot(self,g,u,cid):
        from activities import GameError
        with self.tx():
            row=self.db.execute('SELECT * FROM inventory_chests WHERE id=? AND guild=? AND uid=?',(cid,g,u)).fetchone()
            if not row:raise GameError('Это не твой сундук.')
            if row['opened']:raise GameError('Этот сундук уже открыт. Награда сохранена, повторной нет.')
            if random.random()<0.20:
                outfit=random.choice(list(RARE_OUTFITS))
                if outfit not in self.owned(g,u):
                    self.db.execute('INSERT INTO owned VALUES(?,?,?)',(g,u,outfit))
                    result=dict(kind='outfit',outfit=outfit,text='Редкий образ: '+RARE_OUTFITS[outfit])
                else:
                    coins,xp=self._reward(g,u,150,40,False)
                    result=dict(kind='duplicate',coins=coins,xp=xp,text=f'Повтор образа «{RARE_OUTFITS[outfit]}»: +{coins} монет и +{xp} опыта.')
            else:
                coins,xp=self._reward(g,u,random.randint(50,120),random.randint(15,35),False)
                result=dict(kind='reward',coins=coins,xp=xp,text=f'+{coins} монет и +{xp} опыта — сверх игрового лимита.')
            self.db.execute('UPDATE inventory_chests SET opened=1,result=? WHERE id=?',(json.dumps(result,ensure_ascii=False),cid))
            return result

    def start_maze(self,g,u,channel):
        with self.tx():
            active=self.db.execute('SELECT id FROM maze_runs WHERE guild=? AND uid=? AND done=0 AND expires>?',(g,u,self.clock())).fetchone()
            if active:return self.maze(active['id'])
            self._cooldown(g,u,'maze',600)
            # Randomized depth-first carving gives a connected perfect maze.
            grid=[[1]*9 for _ in range(9)];grid[1][1]=0;stack=[(1,1)]
            while stack:
                x,y=stack[-1]
                choices=[(dx,dy) for dx,dy in [(2,0),(-2,0),(0,2),(0,-2)] if 0<x+dx<8 and 0<y+dy<8 and grid[y+dy][x+dx]]
                if not choices:stack.pop();continue
                dx,dy=random.choice(choices);grid[y+dy//2][x+dx//2]=0;grid[y+dy][x+dx]=0;stack.append((x+dx,y+dy))
            mid=uuid.uuid4().hex
            self.db.execute('INSERT INTO maze_runs(id,guild,uid,grid,x,y,expires,channel) VALUES(?,?,?,?,1,1,?,?)',
                (mid,g,u,json.dumps(grid),self.clock()+600,channel))
            return self.maze(mid)

    def maze(self,mid):
        from activities import GameError
        row=self.db.execute('SELECT * FROM maze_runs WHERE id=?',(mid,)).fetchone()
        if not row:raise GameError('Лабиринт не найден.')
        result=dict(row);result['grid']=json.loads(result['grid']);return result

    def move_maze(self,g,u,channel,message,mid,direction):
        from activities import GameError
        with self.tx():
            m=self.maze(mid)
            if (m['guild'],m['uid'],m['channel'],m['message'])!=(g,u,channel,message):raise GameError('Открой свой /лабиринт.')
            if m['done'] or self.clock()>=m['expires']:raise GameError('Этот поход уже завершён.')
            dx,dy={'n':(0,-1),'s':(0,1),'w':(-1,0),'e':(1,0)}[direction]
            x,y=m['x']+dx,m['y']+dy
            if not (0<=x<9 and 0<=y<9) or m['grid'][y][x]:raise GameError('Там стена. Выбери другой путь.')
            done=(x,y)==(7,7)
            self.db.execute('UPDATE maze_runs SET x=?,y=?,moves=moves+1,done=? WHERE id=?',(x,y,int(done),mid))
            reward=self._reward(g,u,40,25) if done else None
            return self.maze(mid),reward
