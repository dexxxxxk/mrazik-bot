"""SQLite game engine. Transactions contain no network calls or awaits."""
import random
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from content import OUTFITS, QUESTS, TITLES
from activities import ActivitiesMixin, GameError


from quiz_history import QuizHistoryMixin
from personal_pets import PersonalPetsMixin
from adventures import AdventuresMixin


class Game(ActivitiesMixin, QuizHistoryMixin, PersonalPetsMixin, AdventuresMixin):
    def __init__(self, path='data/gnid.sqlite3', tz='Europe/Moscow', clock=time.time):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=5000')
        self.tz, self.clock = ZoneInfo(tz), clock
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS users(
          guild INTEGER, uid INTEGER, coins INTEGER DEFAULT 0 CHECK(coins>=0),
          xp INTEGER DEFAULT 0, day TEXT DEFAULT '', streak INTEGER DEFAULT 0,
          opted INTEGER DEFAULT 0, outfit TEXT DEFAULT 'base', wins INTEGER DEFAULT 0,
          correct INTEGER DEFAULT 0, care INTEGER DEFAULT 0, PRIMARY KEY(guild,uid));
        CREATE TABLE IF NOT EXISTS owned(guild INTEGER, uid INTEGER, item TEXT,
          PRIMARY KEY(guild,uid,item));
        CREATE TABLE IF NOT EXISTS cooldowns(guild INTEGER, uid INTEGER, action TEXT,
          until REAL, PRIMARY KEY(guild,uid,action));
        CREATE TABLE IF NOT EXISTS earnings(guild INTEGER, uid INTEGER, day TEXT,
          coins INTEGER DEFAULT 0, xp INTEGER DEFAULT 0, PRIMARY KEY(guild,uid,day));
        CREATE TABLE IF NOT EXISTS weekly(guild INTEGER, uid INTEGER, week TEXT,
          xp INTEGER DEFAULT 0, PRIMARY KEY(guild,uid,week));
        CREATE TABLE IF NOT EXISTS pets(guild INTEGER PRIMARY KEY, food REAL DEFAULT 70,
          mood REAL DEFAULT 70, energy REAL DEFAULT 70, updated REAL, xp INTEGER DEFAULT 0,
          outfit TEXT DEFAULT 'base', dressed_until REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS settings(guild INTEGER PRIMARY KEY,
          channel INTEGER, hour INTEGER DEFAULT 12, automatic INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS posts(guild INTEGER, day TEXT, PRIMARY KEY(guild,day));
        CREATE TABLE IF NOT EXISTS titles(guild INTEGER, day TEXT, uid INTEGER, title TEXT,
          PRIMARY KEY(guild,day));
        CREATE TABLE IF NOT EXISTS submissions(id INTEGER PRIMARY KEY AUTOINCREMENT,
          guild INTEGER, uid INTEGER, day TEXT, body TEXT, url TEXT, status TEXT DEFAULT 'pending',
          reviewer INTEGER, UNIQUE(guild,uid,day));
        ''')
        self.init_v2()
        self.init_quiz_history()
        self.init_personal_pets()
        self.init_adventures()

    @contextmanager
    def tx(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def now(self):
        return datetime.fromtimestamp(self.clock(), self.tz)

    def day(self):
        return self.now().date().isoformat()

    def week(self):
        d = self.now().date()
        return (d - timedelta(days=d.weekday())).isoformat()

    def user(self, g, u):
        self.db.execute('INSERT OR IGNORE INTO users(guild,uid) VALUES(?,?)', (g, u))
        return dict(self.db.execute('SELECT * FROM users WHERE guild=? AND uid=?', (g,u)).fetchone())

    def owned(self, g, u):
        return ['base'] + [r[0] for r in self.db.execute(
            'SELECT item FROM owned WHERE guild=? AND uid=? ORDER BY item', (g,u))]

    def _cooldown(self, g, u, action, seconds):
        r = self.db.execute('SELECT until FROM cooldowns WHERE guild=? AND uid=? AND action=?',
                            (g,u,action)).fetchone()
        if r and r[0] > self.clock():
            raise GameError(f'Мразик отдыхает. Повтори через {int(r[0]-self.clock())+1} сек.')
        self.db.execute('INSERT OR REPLACE INTO cooldowns VALUES(?,?,?,?)',
                        (g,u,action,self.clock()+seconds))

    def cooldown(self, g, u, action, seconds):
        with self.tx():
            self._cooldown(g,u,action,seconds)

    def _reward(self, g, u, coins, xp, capped=True):
        self.user(g,u)
        self.db.execute('INSERT OR IGNORE INTO earnings(guild,uid,day) VALUES(?,?,?)', (g,u,self.day()))
        row = self.db.execute('SELECT coins,xp FROM earnings WHERE guild=? AND uid=? AND day=?',
                              (g,u,self.day())).fetchone()
        if capped:
            coins, xp = min(coins,max(0,300-row[0])), min(xp,max(0,200-row[1]))
            self.db.execute('UPDATE earnings SET coins=coins+?,xp=xp+? WHERE guild=? AND uid=? AND day=?',
                            (coins,xp,g,u,self.day()))
        self.db.execute('UPDATE users SET coins=coins+?,xp=xp+? WHERE guild=? AND uid=?', (coins,xp,g,u))
        self.db.execute('''INSERT INTO weekly VALUES(?,?,?,?) ON CONFLICT(guild,uid,week)
            DO UPDATE SET xp=xp+excluded.xp''', (g,u,self.week(),xp))
        self.queue_role(g,u)
        return coins,xp

    def daily(self, g, u):
        with self.tx():
            user = self.user(g,u)
            if user['day'] == self.day():
                raise GameError('Сегодня подачка уже выдана. Возвращайся завтра.')
            yesterday = (self.now().date()-timedelta(days=1)).isoformat()
            streak = user['streak']+1 if user['day'] == yesterday else 1
            self.db.execute('UPDATE users SET day=?,streak=? WHERE guild=? AND uid=?',
                            (self.day(),streak,g,u))
            return self._reward(g,u,60+min(streak-1,6)*10,20,False),streak

    def purchase(self, g, u, item):
        if item not in OUTFITS or item == 'base' or OUTFITS.get(item,{}).get('rare'):
            raise GameError('Выбери костюм из магазина.')
        with self.tx():
            user, info = self.user(g,u), OUTFITS[item]
            if item in self.owned(g,u): raise GameError('Эта тряпка уже твоя.')
            if user['xp'] < info['xp']: raise GameError(f"Нужно {info['xp']} опыта. Пока не дорос.")
            if user['coins'] < info['price']: raise GameError('Монеток не хватает. Мразик в долг не даёт.')
            self.db.execute('UPDATE users SET coins=coins-? WHERE guild=? AND uid=?', (info['price'],g,u))
            self.db.execute('INSERT INTO owned VALUES(?,?,?)', (g,u,item))

    def equip(self, g, u, item, shared=False):
        # Legacy argument retained for old call sites; all dress operations are personal now.
        with self.tx():
            self.user(g,u)
            if item not in self.owned(g,u): raise GameError('Сначала купи этот костюм.')
            self.db.execute('UPDATE users SET outfit=? WHERE guild=? AND uid=?',(item,g,u))

    def care(self, g, u, action):
        if action not in ('feed','play','sleep'): raise GameError('Неизвестное действие.')
        with self.tx():
            self.user(g,u)
            self._cooldown(g,u,'care',900)
            p = self.pet(g,u)
            if action=='feed':
                if p['food']>85: raise GameError('Не пихай. Мразик уже сыт.')
                p['food']=min(100,p['food']+20)
            elif action=='play':
                if p['energy']<15: raise GameError('Сил нет. Уложи эту мразоту спать.')
                p['mood']=min(100,p['mood']+20)
                p['energy']-=15
            else:
                if p['energy']>80: raise GameError('Мразик уже выспался и планирует пакости.')
                p['energy']=min(100,p['energy']+30)
            self.db.execute('UPDATE personal_pets SET food=?,mood=?,energy=?,updated=? WHERE guild=? AND uid=?',
                            (p['food'],p['mood'],p['energy'],self.clock(),g,u))
            self.db.execute('UPDATE users SET care=care+1 WHERE guild=? AND uid=?', (g,u))
            return self._reward(g,u,10,10)

    def quiz_reward(self, g, u, correct):
        with self.tx():
            if correct:
                self.user(g,u)
                self.db.execute('UPDATE users SET correct=correct+1 WHERE guild=? AND uid=?',(g,u))
            return self._reward(g,u,25 if correct else 0,15 if correct else 2)

    def duel_reward(self, g, winner, loser):
        with self.tx():
            self.user(g,winner)
            self.db.execute('UPDATE users SET wins=wins+1 WHERE guild=? AND uid=?',(g,winner))
            return self._reward(g,winner,30,20),self._reward(g,loser,5,5)

    def expedition(self, g, u):
        with self.tx():
            self._cooldown(g,u,'expedition',4*3600)
            event,coins = random.choice([
                ('Нашёл кошелёк. Пустой. Но под ним лежали монетки.',35),
                ('Продал ржавую ложку как древний артефакт.',55),
                ('Поссорился с голубем. Голубь откупился.',25),
                ('Обнаружил клад под собственным матрасом.',80),
                ('Вернулся с носком. За старание держи мелочь.',15)])
            return event,self._reward(g,u,coins,20)

    def opt(self, g, u, enabled):
        with self.tx():
            self.user(g,u)
            self.db.execute('UPDATE users SET opted=? WHERE guild=? AND uid=?',(int(enabled),g,u))
            if not enabled:
                self.db.execute('DELETE FROM titles WHERE guild=? AND uid=?',(g,u))

    def title(self, g):
        with self.tx():
            existing = self.db.execute('SELECT uid,title FROM titles WHERE guild=? AND day=?',(g,self.day())).fetchone()
            if existing: return tuple(existing)
            users = [r[0] for r in self.db.execute('SELECT uid FROM users WHERE guild=? AND opted=1',(g,))]
            if not users: raise GameError('Участников пока нет. Вступить: /участие включить:True')
            u,title = random.choice(users),random.choice(TITLES)
            self.db.execute('INSERT INTO titles VALUES(?,?,?,?)',(g,self.day(),u,title))
            return u,title

    def quest(self, day=None):
        d = datetime.fromisoformat(day or self.day()).date()
        return QUESTS[d.toordinal()%len(QUESTS)]

    def submit(self, g, u, body, url):
        with self.tx():
            self.user(g,u)
            try:
                cur = self.db.execute('INSERT INTO submissions(guild,uid,day,body,url) VALUES(?,?,?,?,?)',
                                     (g,u,self.day(),body,url))
            except sqlite3.IntegrityError:
                raise GameError('Сегодня работа уже подана. Повторно сдавать нельзя.') from None
            return cur.lastrowid

    def review(self, g, reviewer, sid, approved):
        with self.tx():
            s = self.db.execute('SELECT * FROM submissions WHERE guild=? AND id=?',(g,sid)).fetchone()
            if not s or s['status']!='pending': raise GameError('Работа не найдена или уже проверена.')
            if s['uid']==reviewer: raise GameError('Свою работу проверять нельзя. Нужен другой модератор.')
            self.db.execute('UPDATE submissions SET status=?,reviewer=? WHERE id=?',
                            ('approved' if approved else 'rejected',reviewer,sid))
            reward = self._reward(g,s['uid'],80,40,False) if approved else (0,0)
            return s['uid'],reward

    def top(self, g, weekly=True):
        if weekly:
            return self.db.execute('SELECT uid,xp FROM weekly WHERE guild=? AND week=? AND xp>0 ORDER BY xp DESC,uid LIMIT 10',
                                   (g,self.week())).fetchall()
        return self.db.execute('SELECT uid,xp FROM users WHERE guild=? ORDER BY xp DESC,uid LIMIT 10',(g,)).fetchall()

    def settings(self, g):
        r = self.db.execute('SELECT * FROM settings WHERE guild=?',(g,)).fetchone()
        return dict(r) if r else None

    def configure(self, g, channel, hour, automatic):
        self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?,?,?)',(g,channel,hour,int(automatic)))

    def claim_post(self, g):
        try:
            self.db.execute('INSERT INTO posts VALUES(?,?)',(g,self.day()))
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_user(self, g, u):
        with self.tx():
            for table in ['users','owned','cooldowns','earnings','weekly','submissions','titles','personal_pets','quiz_seen','quiz_daily','role_queue']:
                self.db.execute(f'DELETE FROM {table} WHERE guild=? AND uid=?',(g,u))
