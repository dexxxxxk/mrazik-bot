"""No-repeat quiz selection, shared by personal and public games."""
import hashlib
import json
import random
from content import QUIZ


def question_id(text):
    # Stable across bank reorderings and additions; never use the list position.
    return hashlib.sha256(text.strip().encode('utf-8')).hexdigest()


class QuizHistoryMixin:
    def init_quiz_history(self):
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS quiz_seen(guild INTEGER,uid INTEGER,qid TEXT,event TEXT,
            PRIMARY KEY(guild,uid,qid));
        CREATE TABLE IF NOT EXISTS quiz_public_seen(guild INTEGER,qid TEXT,PRIMARY KEY(guild,qid));
        CREATE TABLE IF NOT EXISTS quiz_daily(guild INTEGER,uid INTEGER,day TEXT,count INTEGER DEFAULT 0,
            PRIMARY KEY(guild,uid,day));
        CREATE TABLE IF NOT EXISTS migrations(name TEXT PRIMARY KEY);
        ''')
        with self.tx():
            if not self.db.execute("SELECT 1 FROM migrations WHERE name='quiz_history_2_1'").fetchone():
                for e in self.db.execute("SELECT * FROM events WHERE kind='quiz'").fetchall():
                    p=json.loads(e['payload']); qid=p.get('qid') or question_id(p['text'])
                    if not e['owner']:
                        self.db.execute('INSERT OR IGNORE INTO quiz_public_seen VALUES(?,?)',(e['guild'],qid))
                    else:
                        self.db.execute('INSERT OR IGNORE INTO quiz_seen VALUES(?,?,?,?)',(e['guild'],e['owner'],qid,e['id']))
                    for row in self.db.execute('SELECT uid FROM event_claims WHERE event=?',(e['id'],)).fetchall():
                        self.db.execute('INSERT OR IGNORE INTO quiz_seen VALUES(?,?,?,?)',(e['guild'],row['uid'],qid,e['id']))
                self.db.execute("INSERT INTO migrations VALUES('quiz_history_2_1')")

    def available_questions(self,g,u=0):
        seen={r[0] for r in self.db.execute('SELECT qid FROM quiz_public_seen WHERE guild=?',(g,))}
        if u:
            seen.update(r[0] for r in self.db.execute('SELECT qid FROM quiz_seen WHERE guild=? AND uid=?',(g,u)))
        return [q for q in QUIZ if question_id(q[0]) not in seen]

    def select_question(self,g,u,eid):
        from activities import GameError
        remaining=self.available_questions(g,u)
        if not remaining:
            raise GameError('Новые вопросы закончились. Повторы не включаю. Попробуй другие игры; новые вопросы появятся с обновлением банка.')
        q=random.choice(remaining); qid=question_id(q[0])
        if u: self.db.execute('INSERT INTO quiz_seen VALUES(?,?,?,?)',(g,u,qid,eid))
        else: self.db.execute('INSERT INTO quiz_public_seen VALUES(?,?)',(g,qid))
        return q,qid

    def reserve_quiz_attempt(self,e,u):
        from activities import GameError
        qid=e['payload'].get('qid') or question_id(e['payload']['text'])
        seen=self.db.execute('SELECT event FROM quiz_seen WHERE guild=? AND uid=? AND qid=?',(e['guild'],u,qid)).fetchone()
        if seen and seen['event']!=e['id']:
            raise GameError('Этот вопрос тебе уже попадался. Повторной попытки и награды нет.')
        self.db.execute('INSERT OR IGNORE INTO quiz_seen VALUES(?,?,?,?)',(e['guild'],u,qid,e['id']))
