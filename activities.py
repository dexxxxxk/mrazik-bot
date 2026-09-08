"""Persistent v2 events, personal mini-games and role synchronization queue."""
import json
import random
import uuid
from content import QUIZ, RANKS


class GameError(Exception):
    pass


class ActivitiesMixin:
    def init_v2(self):
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS role_settings(guild INTEGER PRIMARY KEY, enabled INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS rank_roles(guild INTEGER, threshold INTEGER, role INTEGER,
          PRIMARY KEY(guild,threshold), UNIQUE(guild,role));
        CREATE TABLE IF NOT EXISTS role_queue(guild INTEGER, uid INTEGER, xp INTEGER,
          retry_at REAL DEFAULT 0, PRIMARY KEY(guild,uid));
        CREATE TABLE IF NOT EXISTS event_settings(guild INTEGER PRIMARY KEY, enabled INTEGER DEFAULT 1,
          next_at REAL, quiet_start INTEGER DEFAULT 0, quiet_end INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, guild INTEGER, channel INTEGER,
          kind TEXT, payload TEXT, owner INTEGER DEFAULT 0, starts REAL, expires REAL,
          message INTEGER DEFAULT 0, state TEXT DEFAULT 'pending', summary_done INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS event_claims(event TEXT, uid INTEGER, correct INTEGER,
          coins INTEGER, xp INTEGER, PRIMARY KEY(event,uid));
        CREATE INDEX IF NOT EXISTS event_expiry ON events(state,expires);
        ''')

    def queue_role(self, g, u):
        xp = self.user(g,u)['xp']
        self.db.execute('''INSERT INTO role_queue(guild,uid,xp,retry_at) VALUES(?,?,?,0)
            ON CONFLICT(guild,uid) DO UPDATE SET xp=excluded.xp,retry_at=0''',(g,u,xp))

    def rank_role_map(self,g):
        return dict(self.db.execute('SELECT threshold,role FROM rank_roles WHERE guild=? ORDER BY threshold',(g,)))

    def remember_role(self,g,threshold,role):
        if threshold not in dict(RANKS): raise GameError('Неизвестный ранг.')
        self.db.execute('INSERT OR REPLACE INTO rank_roles VALUES(?,?,?)',(g,threshold,role))

    def enable_roles(self,g,enabled):
        with self.tx():
            self.db.execute('INSERT OR REPLACE INTO role_settings VALUES(?,?)',(g,int(enabled)))
            if enabled:
                self.db.execute('''INSERT INTO role_queue(guild,uid,xp,retry_at)
                    SELECT guild,uid,xp,0 FROM users WHERE guild=?
                    ON CONFLICT(guild,uid) DO UPDATE SET xp=excluded.xp,retry_at=0''',(g,))

    def roles_enabled(self,g):
        r=self.db.execute('SELECT enabled FROM role_settings WHERE guild=?',(g,)).fetchone()
        return bool(r and r[0])

    def role_ack(self,g,u,xp):
        self.db.execute('DELETE FROM role_queue WHERE guild=? AND uid=? AND xp=?',(g,u,xp))

    def ensure_event_settings(self,g):
        # First event: a random future minute within one hour. Thereafter one slot per hour.
        self.db.execute('INSERT OR IGNORE INTO event_settings(guild,next_at) VALUES(?,?)',
                        (g,self.clock()+random.randint(300,3300)))
        return dict(self.db.execute('SELECT * FROM event_settings WHERE guild=?',(g,)).fetchone())

    def configure_events(self,g,enabled,quiet_start=0,quiet_end=0):
        if not 0<=quiet_start<=23 or not 0<=quiet_end<=23: raise GameError('Час должен быть от 0 до 23.')
        with self.tx():
            self.ensure_event_settings(g)
            self.db.execute('UPDATE event_settings SET enabled=?,quiet_start=?,quiet_end=? WHERE guild=?',
                            (int(enabled),quiet_start,quiet_end,g))
            if not enabled:
                self.db.execute("UPDATE events SET expires=? WHERE guild=? AND owner=0 AND state='open'",
                                (self.clock(),g))

    def in_quiet_hours(self,cfg):
        start,end,h=cfg['quiet_start'],cfg['quiet_end'],self.now().hour
        if start==end: return False
        return start<=h<end if start<end else h>=start or h<end

    def _create_event(self,g,channel,kind,owner=0):
        now=self.clock()
        eid=uuid.uuid4().hex
        if kind=='quiz':
            (question,opts,answer),qid=self.select_question(g,owner,eid)
            opts=random.sample(opts,len(opts))
            payload=dict(title='Мразик устроил викторину',text=question,options=opts,
                         answer=opts.index(answer),qid=qid,coins=25,xp=15,art='mage')
        elif kind=='stash':
            payload=dict(title='Мразик рассыпал заначку!',text='Подбирай монетки, пока он не заметил.',
                         options=['🪙 Подобрать монетки'],answer=0,coins=20,xp=12,art='pirate')
        elif kind=='target':
            opts=random.sample(['🍄','🧦','🪳','🥒'],4); answer=random.randrange(4)
            payload=dict(title='Поймай пакость',text=f'Нажми **{opts[answer]}**. Одна попытка!',
                         options=opts,answer=answer,coins=20,xp=12,art='cyber')
        elif kind=='chests':
            amounts=random.sample([10,30,60],3)
            payload=dict(title='Три подозрительных сундука',text='Выбери один. Вход бесплатный, монетки не теряются.',
                         options=['📦 Левый','📦 Средний','📦 Правый'],amounts=amounts,xp=12,art='pirate')
        elif kind=='guess':
            payload=dict(title='Что загадал Мразик?',text='Угадай число от 1 до 5. Одна попытка.',
                         options=['1','2','3','4','5'],answer=random.randrange(5),coins=45,xp=20,art='vampire')
        elif kind=='fish':
            payload=dict(title='Рыбалка в подозрительной луже',text='Подсекай после указанного времени. На это будет 20 секунд.',
                         options=['🎣 Подсечь'],answer=0,coins=random.choice([10,20,35,50]),xp=15,art='beach')
            now+=random.randint(5,12)
        else: raise GameError('Неизвестная игра.')
        expires=now+(20 if kind=='fish' else (45 if kind=='quiz' and owner else (120 if owner else 300)))
        self.db.execute('INSERT INTO events(id,guild,channel,kind,payload,owner,starts,expires) VALUES(?,?,?,?,?,?,?,?)',
                        (eid,g,channel,kind,json.dumps(payload,ensure_ascii=False),owner,now,expires))
        return self.event(eid)

    def claim_scheduled_event(self,g,channel):
        with self.tx():
            cfg=self.ensure_event_settings(g)
            if not cfg['enabled'] or cfg['next_at']>self.clock(): return None
            # Advance before sending; neither downtime nor repeated ticks can flood the channel.
            next_at=(int(self.clock()//3600)+1)*3600+random.randint(300,3300)
            self.db.execute('UPDATE event_settings SET next_at=? WHERE guild=?',(next_at,g))
            if self.in_quiet_hours(cfg): return None
            active=self.db.execute("SELECT 1 FROM events WHERE guild=? AND owner=0 AND state IN ('pending','open') AND expires>?",
                                   (g,self.clock())).fetchone()
            if active: return None
            kinds=['stash','target']+(['quiz'] if self.available_questions(g) else [])
            return self._create_event(g,channel,random.choice(kinds))

    def start_activity(self,g,u,channel,kind,public=False):
        with self.tx():
            if public:
                if kind not in ('quiz','stash','target'): raise GameError('Эта игра личная.')
                self._cooldown(g,0,'manual_event',600)
                active=self.db.execute("SELECT 1 FROM events WHERE guild=? AND owner=0 AND state IN ('pending','open') AND expires>?",
                                       (g,self.clock())).fetchone()
                if active: raise GameError('В канале уже идёт событие. Дождись окончания.')
            else:
                if kind not in ('quiz','chests','guess','fish'): raise GameError('Неизвестная личная игра.')
                if kind=='quiz':
                    used=self.db.execute('SELECT count FROM quiz_daily WHERE guild=? AND uid=? AND day=?',(g,u,self.day())).fetchone()
                    if used and used[0]>=3: raise GameError('Три личных вопроса на сегодня уже открыты. Возвращайся завтра; другие игры доступны.')
                    self._cooldown(g,u,'quiz',600)
                    self.db.execute('INSERT INTO quiz_daily VALUES(?,?,?,1) ON CONFLICT(guild,uid,day) DO UPDATE SET count=count+1',(g,u,self.day()))
                else:
                    self._cooldown(g,u,'mini:'+kind,300 if kind=='chests' else 120)
            return self._create_event(g,channel,kind,0 if public else u)

    def event(self,eid):
        r=self.db.execute('SELECT * FROM events WHERE id=?',(eid,)).fetchone()
        if not r: raise GameError('Событие не найдено.')
        result=dict(r); result['payload']=json.loads(result['payload'])
        return result

    def open_event(self,eid,message):
        self.db.execute("UPDATE events SET message=?,state='open' WHERE id=? AND state='pending'",(message,eid))

    def fail_event(self,eid):
        self.db.execute("UPDATE events SET state='failed' WHERE id=? AND state='pending'",(eid,))

    def answer_activity(self,g,u,channel,message,eid,choice):
        with self.tx():
            e=self.event(eid); p=e['payload']
            if e['guild']!=g or e['channel']!=channel or e['message']!=message:
                raise GameError('Эта кнопка от другого события.')
            if e['owner'] and e['owner']!=u: raise GameError('Это чужая игра. Открой свою командой.')
            if e['state']!='open' or self.clock()>=e['expires']: raise GameError('Время вышло. Мразик всё унёс.')
            if self.clock()<e['starts']: raise GameError('Рано! Подожди до указанного времени поклёвки.')
            if not 0<=choice<len(p['options']): raise GameError('Неизвестная кнопка.')
            if self.db.execute('SELECT 1 FROM event_claims WHERE event=? AND uid=?',(eid,u)).fetchone():
                raise GameError('Твоя попытка уже использована. Награду дважды не выдаю.')
            if e['kind']=='quiz': self.reserve_quiz_attempt(e,u)
            correct=e['kind']=='chests' or choice==p['answer']
            coins=p['amounts'][choice] if e['kind']=='chests' else (p['coins'] if correct else 0)
            xp=p['xp'] if correct else 2
            if e['kind']=='quiz' and correct:
                self.user(g,u)
                self.db.execute('UPDATE users SET correct=correct+1 WHERE guild=? AND uid=?',(g,u))
            reward=self._reward(g,u,coins,xp)
            self.db.execute('INSERT INTO event_claims VALUES(?,?,?,?,?)',(eid,u,int(correct),*reward))
            if e['owner']: self.db.execute("UPDATE events SET state='closed',summary_done=1 WHERE id=?",(eid,))
            if e['kind']=='chests': phrase=f'В сундуке оказалось {coins} монеток.'
            elif e['kind']=='fish': phrase=random.choice(['Выудил ржавую корону. Продал коллекционеру.', 'Поймал сапог с заначкой.','Улов пахнет странно, зато платят.'])
            else: phrase='Есть! Мразик нехотя делится.' if correct else 'Мимо. Мразик злорадствует.'
            return reward,correct,phrase

    def expire_events(self):
        self.db.execute("UPDATE events SET state='closed' WHERE state IN ('pending','open') AND expires<=?",(self.clock(),))

    def event_stats(self,eid):
        r=self.db.execute('SELECT count(*),coalesce(sum(correct),0) FROM event_claims WHERE event=?',(eid,)).fetchone()
        return tuple(r)
