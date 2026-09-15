"""Daily cooperative boss. All combat and payout changes are one SQLite transaction."""
import random
import sqlite3
import uuid
from datetime import datetime,time,timedelta
from pathlib import Path
from game import GameError

BOSSES=[('Гнилоклык, пожиратель припасов','dragon'),('Лич просроченного супа','lich'),
        ('Герцог помойного пламени','demon'),('Грибной владыка подвала','mushroom')]

class BossStore:
    def __init__(self,game):
        self.game,self.db=game,game.db
        if not self.db.execute("SELECT 1 FROM migrations WHERE name='daily_boss_v5'").fetchone():
            filename=self.db.execute('PRAGMA database_list').fetchone()[2]
            if filename and self.db.execute('SELECT 1 FROM users LIMIT 1').fetchone():
                folder=Path(filename).parent/'backups';folder.mkdir(exist_ok=True)
                dest=sqlite3.connect(folder/f'pre-daily-boss-{uuid.uuid4().hex}.sqlite3')
                try:self.db.backup(dest)
                finally:dest.close()
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS boss_settings(guild INTEGER PRIMARY KEY,enabled INTEGER DEFAULT 1,
            hour INTEGER DEFAULT 12,health INTEGER DEFAULT 1500,error TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS boss_runs(id TEXT PRIMARY KEY,guild INTEGER,day TEXT,channel INTEGER,
            message INTEGER DEFAULT 0,name TEXT,art TEXT,hp INTEGER,max_hp INTEGER,state TEXT DEFAULT 'pending',
            created REAL,expires REAL,finished REAL DEFAULT 0,cleanup_at REAL DEFAULT 0,
            cleaned INTEGER DEFAULT 0,retry_at REAL DEFAULT 0,dirty INTEGER DEFAULT 1,UNIQUE(guild,day));
        CREATE TABLE IF NOT EXISTS boss_hits(boss TEXT,uid INTEGER,damage INTEGER DEFAULT 0,
            hits INTEGER DEFAULT 0,last_hit REAL DEFAULT 0,coins INTEGER DEFAULT 0,xp INTEGER DEFAULT 0,
            PRIMARY KEY(boss,uid));
        ''')
        self.db.execute("INSERT OR IGNORE INTO migrations VALUES('daily_boss_v5')")

    def settings(self,g):
        self.db.execute('INSERT OR IGNORE INTO boss_settings(guild) VALUES(?)',(g,))
        return dict(self.db.execute('SELECT * FROM boss_settings WHERE guild=?',(g,)).fetchone())

    def configure(self,g,enabled,hour,health):
        if not 0<=hour<=23 or not 300<=health<=100000:raise GameError('Час: 0–23, здоровье: 300–100000.')
        with self.game.tx():
            self.settings(g)
            self.db.execute('UPDATE boss_settings SET enabled=?,hour=?,health=? WHERE guild=?',(int(enabled),hour,health,g))

    def get(self,bid):
        row=self.db.execute('SELECT * FROM boss_runs WHERE id=?',(bid,)).fetchone()
        if not row:raise GameError('Босс не найден.')
        return dict(row)

    def today(self,g):
        row=self.db.execute('SELECT id FROM boss_runs WHERE guild=? AND day=?',(g,self.game.day())).fetchone()
        return self.get(row['id']) if row else None

    def next_spawn(self,g):
        cfg=self.settings(g);now=self.game.now()
        day=now.date()+(timedelta(days=1) if self.today(g) else timedelta())
        planned=datetime.combine(day,time(cfg['hour']),tzinfo=self.game.tz)
        return max(planned.timestamp(),self.game.clock())

    def expire(self):
        now=self.game.clock()
        self.db.execute("UPDATE boss_runs SET state='escaped',finished=?,cleanup_at=?,dirty=1 WHERE state IN ('pending','open') AND expires<=?",(now,now+3600,now))

    def spawn(self,g,force=False):
        with self.game.tx():
            self.expire();cfg=self.settings(g);channel=self.game.settings(g)
            if not channel:raise GameError('Сначала выбери игровой канал через /настройка.')
            existing=self.today(g)
            if existing:return existing
            if not cfg['enabled'] and not force:return None
            if not force and self.game.now().hour<cfg['hour']:return None
            now=self.game.clock();bid=uuid.uuid4().hex;name,art=random.choice(BOSSES)
            expires=datetime.combine(self.game.now().date()+timedelta(days=1),time.min,tzinfo=self.game.tz).timestamp()
            self.db.execute('INSERT INTO boss_runs(id,guild,day,channel,name,art,hp,max_hp,created,expires) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (bid,g,self.game.day(),channel['channel'],name,art,cfg['health'],cfg['health'],now,expires))
            return self.get(bid)

    def members(self,bid):
        return [dict(r) for r in self.db.execute('SELECT * FROM boss_hits WHERE boss=? ORDER BY damage DESC,uid',(bid,))]

    def hit(self,g,u,ch,message,bid):
        with self.game.tx():
            b=self.get(bid);configured=self.game.settings(g)
            if not configured or configured['channel']!=ch or (b['guild'],b['channel'],b['message'])!=(g,ch,message):
                raise GameError('Открой актуальную карточку босса в игровом канале.')
            if b['state']!='open' or b['expires']<=self.game.clock():raise GameError('Бой уже завершён. Следующий босс появится в другой день.')
            old=self.db.execute('SELECT * FROM boss_hits WHERE boss=? AND uid=?',(bid,u)).fetchone()
            if old and self.game.clock()<old['last_hit']+3600:
                raise GameError(f"Твой следующий удар <t:{int(old['last_hit']+3600)}:R>. Пауза — один час.")
            damage=min(b['hp'],random.randint(80,140));remaining=b['hp']-damage
            self.game.user(g,u)
            self.db.execute('INSERT INTO boss_hits(boss,uid,damage,hits,last_hit) VALUES(?,?,?,1,?) ON CONFLICT(boss,uid) DO UPDATE SET damage=damage+excluded.damage,hits=hits+1,last_hit=excluded.last_hit',
                (bid,u,damage,self.game.clock()))
            self.db.execute('UPDATE boss_runs SET hp=?,dirty=1 WHERE id=?',(remaining,bid))
            if remaining==0:
                # Completing the fight and paying EVERY participant commit together.
                for row in self.members(bid):
                    coins=100+300*row['damage']//b['max_hp'];xp=60+180*row['damage']//b['max_hp']
                    paid=self.game._reward(g,row['uid'],coins,xp,False)
                    self.db.execute('UPDATE boss_hits SET coins=?,xp=? WHERE boss=? AND uid=?',(*paid,bid,row['uid']))
                now=self.game.clock()
                self.db.execute("UPDATE boss_runs SET state='defeated',finished=?,cleanup_at=? WHERE id=?",(now,now+3600,bid))
            return damage,self.get(bid)
