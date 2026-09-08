"""Additive migration to one pet per member. XP and outfit keep their original storage."""
import sqlite3
import uuid
from pathlib import Path


class PersonalPetsMixin:
    def init_personal_pets(self):
        migrated=self.db.execute("SELECT 1 FROM migrations WHERE name='personal_pets_v3'").fetchone()
        if not migrated:
            # Back up existing game data before the first migration, using SQLite's consistent API.
            dbfile=self.db.execute('PRAGMA database_list').fetchone()[2]
            if dbfile and self.db.execute('SELECT 1 FROM users LIMIT 1').fetchone():
                folder=Path(dbfile).parent/'backups';folder.mkdir(parents=True,exist_ok=True)
                backup=folder/f'pre-personal-pets-{uuid.uuid4().hex}.sqlite3'
                dest=sqlite3.connect(backup)
                try:self.db.backup(dest)
                finally:dest.close()
        self.db.execute('''CREATE TABLE IF NOT EXISTS personal_pets(
            guild INTEGER,uid INTEGER,food REAL DEFAULT 70,mood REAL DEFAULT 70,
            energy REAL DEFAULT 70,updated REAL,PRIMARY KEY(guild,uid))''')
        if not migrated:
            with self.tx():
                # Existing members receive a snapshot of the former shared pet's wellbeing.
                # Personal XP, balances, equipped outfits and role IDs are never rewritten.
                self.db.execute('''INSERT OR IGNORE INTO personal_pets(guild,uid,food,mood,energy,updated)
                    SELECT u.guild,u.uid,coalesce(p.food,70),coalesce(p.mood,70),coalesce(p.energy,70),coalesce(p.updated,?)
                    FROM users u LEFT JOIN pets p ON p.guild=u.guild''',(self.clock(),))
                self.db.execute("INSERT OR IGNORE INTO migrations VALUES('personal_pets_v3')")

    def pet(self,g,u):
        owner=self.user(g,u)
        self.db.execute('INSERT OR IGNORE INTO personal_pets(guild,uid,updated) VALUES(?,?,?)',(g,u,self.clock()))
        pet=dict(self.db.execute('SELECT * FROM personal_pets WHERE guild=? AND uid=?',(g,u)).fetchone())
        hours=max(0,(self.clock()-pet['updated'])/3600)
        for key,rate in [('food',3),('mood',2),('energy',-4)]:
            pet[key]=max(0,min(100,pet[key]-hours*rate))
        pet.update(xp=owner['xp'],outfit=owner['outfit'])
        return pet
