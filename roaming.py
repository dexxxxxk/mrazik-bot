"""Independent public-channel scheduler, diagnostics and illustrated events."""
import asyncio
import json
import logging
import random
import sqlite3
import uuid
from pathlib import Path
import discord
from discord import app_commands
from discord.ext import tasks
from game import GameError

log=logging.getLogger('mrazik.autopost')
PERMISSIONS={'view_channel':'Просматривать канал','send_messages':'Отправлять сообщения',
             'embed_links':'Встраивать ссылки','attach_files':'Прикреплять файлы'}

def channel_reason(channel,guild,member=None):
    if not channel.permissions_for(guild.default_role).view_channel:return 'канал закрыт для @everyone'
    member=member or guild.me
    if not member:return 'бот не найден среди участников сервера'
    p=channel.permissions_for(member)
    missing=[label for key,label in PERMISSIONS.items() if not getattr(p,key)]
    return 'нет прав бота: '+', '.join(missing) if missing else ''

class Roaming:
    def __init__(self,bot,game):
        self.bot,self.game=bot,game;self.locks={}
        db=game.db
        if not db.execute("SELECT 1 FROM migrations WHERE name='roaming_v42'").fetchone():
            filename=db.execute('PRAGMA database_list').fetchone()[2]
            if filename and db.execute('SELECT 1 FROM users LIMIT 1').fetchone():
                folder=Path(filename).parent/'backups';folder.mkdir(exist_ok=True)
                dest=sqlite3.connect(folder/f'pre-roaming-{uuid.uuid4().hex}.sqlite3')
                try:db.backup(dest)
                finally:dest.close()
        db.execute('''CREATE TABLE IF NOT EXISTS roaming_status(guild INTEGER PRIMARY KEY,
            min_minutes INTEGER DEFAULT 120,max_minutes INTEGER DEFAULT 240,last_attempt REAL DEFAULT 0,
            last_success REAL DEFAULT 0,last_channel INTEGER DEFAULT 0,last_message INTEGER DEFAULT 0,
            last_kind TEXT DEFAULT '',error TEXT DEFAULT '')''')
        db.execute("INSERT OR IGNORE INTO migrations VALUES('roaming_v42')")

    def state(self,g):
        self.game.loot_settings(g)
        self.game.db.execute('INSERT OR IGNORE INTO roaming_status(guild) VALUES(?)',(g,))
        return dict(self.game.db.execute('SELECT * FROM roaming_status JOIN loot_schedule USING(guild) WHERE guild=?',(g,)).fetchone())

    def configure(self,g,enabled,minimum,maximum):
        if not 10<=minimum<=maximum<=1440:raise GameError('Интервал: от 10 до 1440 минут, максимум не меньше минимума.')
        with self.game.tx():
            self.state(g)
            self.game.db.execute('UPDATE roaming_status SET min_minutes=?,max_minutes=? WHERE guild=?',(minimum,maximum,g))
            self.game.db.execute('UPDATE loot_schedule SET enabled=?,next_at=? WHERE guild=?',(int(enabled),self.game.clock()+60,g))

    async def channels(self,guild):
        # Refresh overwrites through REST; failures are reported instead of silently skipping.
        member=guild.me or await guild.fetch_member(self.bot.user.id)
        channels=await guild.fetch_channels()
        return [(c,channel_reason(c,guild,member)) for c in channels if isinstance(c,discord.TextChannel)]

    def create_event(self,g,ch,variant):
        if variant=='drop':return self.game.create_loot_drop(g,ch)
        if variant=='rescue':
            options=['Позвать по имени','Погреметь миской','Показать тапок'];answer=random.randrange(3)
            clue=['Он отзывается только на своё имя.','Из кустов слышно урчание голодного живота.','На ветке застрял его любимый тапок.'][answer]
            p=dict(title='Потерявшийся Мразик',text=clue+' Выбери, чем его приманить.',options=options,answer=answer,coins=45,xp=25,art='detective');kind='target'
        elif variant=='parcel':
            amounts=[25,45,70];random.shuffle(amounts)
            p=dict(title='Почтальон притащил посылки',text='Выбери одну бесплатную посылку. В каждой есть подарок!',options=['Мятая коробка','Шуршащий пакет','Подозрительный свёрток'],amounts=amounts,xp=20,art='postman');kind='chests'
        elif variant=='caravan':
            p=dict(title='Караван потерял груз',text='Помоги шерифу собрать рассыпавшиеся припасы. Награда каждому, кто успеет помочь.',options=['Помочь каравану'],answer=0,coins=35,xp=20,art='cowboy');kind='stash'
        else:raise GameError('Неизвестный тип автопоста.')
        p.update(public_any=True,uncapped=True)
        with self.game.tx():
            eid=uuid.uuid4().hex
            self.game.db.execute('INSERT INTO events(id,guild,channel,kind,payload,starts,expires) VALUES(?,?,?,?,?,?,?)',
                (eid,g,ch,kind,json.dumps(p,ensure_ascii=False),self.game.clock(),self.game.clock()+300))
        return self.game.event(eid)

    async def send(self,guild,force=False):
        async with self.locks.setdefault(guild.id,asyncio.Lock()):
            cfg=self.state(guild.id);now=self.game.clock()
            if not cfg['enabled']:return 'Выключено. Включи /автопосты.'
            if not force and cfg['next_at']>now:return None
            if force and cfg['last_attempt'] and now-cfg['last_attempt']<120:raise GameError('Между проверочными отправками нужно подождать 2 минуты.')
            # Claim a retry slot before awaiting network calls. A reconnect cannot flood channels.
            self.game.db.execute('UPDATE loot_schedule SET next_at=? WHERE guild=?',(now+300,guild.id))
            self.game.db.execute('UPDATE roaming_status SET last_attempt=? WHERE guild=?',(now,guild.id))
            try:
                active=self.game.db.execute("SELECT payload,kind FROM events WHERE guild=? AND owner=0 AND state IN ('pending','open') AND expires>?",(guild.id,now)).fetchall()
                if any(e['kind']=='drop' or json.loads(e['payload']).get('public_any') for e in active):
                    return 'Уже идёт случайное событие. Дождись окончания его пяти минут.'
                candidates=[c for c,reason in await self.channels(guild) if not reason]
                if not candidates:raise GameError('Нет открытых текстовых каналов с нужными правами. Подробности: /автопост_статус.')
                alternatives=[c for c in candidates if c.id!=cfg['last_channel']]
                channel=random.choice(alternatives or candidates)
                variant=random.choice([v for v in ['drop','rescue','parcel','caravan'] if v!=cfg['last_kind']])
                art={'drop':'pharaoh','rescue':'detective','parcel':'postman','caravan':'cowboy'}[variant]
                if not (Path(__file__).parent/'assets'/f'{art}.png').is_file():
                    raise GameError(f'Не загружена картинка assets/{art}.png из версии 4.')
                e=self.create_event(guild.id,channel.id,variant)
                msg=await self.bot.features.publish(channel,e)
                self.game.db.execute('UPDATE roaming_status SET last_success=?,last_channel=?,last_message=?,last_kind=?,error=? WHERE guild=?',
                    (now,channel.id,msg.id,variant,'',guild.id))
                self.game.db.execute('UPDATE loot_schedule SET next_at=? WHERE guild=?',
                    (now+random.randint(cfg['min_minutes']*60,cfg['max_minutes']*60),guild.id))
                log.info('Autopost delivered guild=%s channel=%s kind=%s message=%s',guild.id,channel.id,variant,msg.id)
                return f'Событие отправлено в <#{channel.id}>: https://discord.com/channels/{guild.id}/{channel.id}/{msg.id}'
            except Exception as exc:
                reason=str(exc)[:800]
                self.game.db.execute('UPDATE roaming_status SET error=? WHERE guild=?',(reason,guild.id))
                log.exception('Autopost failed guild=%s; retry in 5 minutes',guild.id)
                if force:raise GameError('Не удалось отправить событие: '+reason) from exc
                return None

    @tasks.loop(seconds=30)
    async def worker(self):
        for row in self.game.db.execute('SELECT guild FROM settings').fetchall():
            guild=self.bot.get_guild(row['guild'])
            if guild:
                try:await self.send(guild)
                except Exception:log.exception('Autopost scheduler failed guild=%s',guild.id)

    @worker.before_loop
    async def before_worker(self):await self.bot.wait_until_ready()

    async def status(self,i):
        await i.response.defer(ephemeral=True)
        cfg=self.state(i.guild_id)
        lines=[f"Автопосты: **{'включены' if cfg['enabled'] else 'выключены'}**.",
               f"Интервал: {cfg['min_minutes']}–{cfg['max_minutes']} минут.",
               f"Следующая попытка: <t:{int(cfg['next_at'])}:R>." if cfg['enabled'] else 'Расписание приостановлено.',
               f"Планировщик: {'работает' if self.worker.is_running() else 'не запущен — проверь логи и версию бота'}."
               ]
        if not self.game.settings(i.guild_id):lines.append('Сначала выбери игровой канал через /настройка.')
        if cfg['last_success']:lines.append(f"Последняя отправка: <t:{int(cfg['last_success'])}:f> в <#{cfg['last_channel']}>. Событие удаляется через 5 минут.")
        missing=[name for name in ['pharaoh','detective','postman','cowboy'] if not (Path(__file__).parent/'assets'/f'{name}.png').is_file()]
        if missing:lines.append('Не хватает картинок в assets: '+', '.join(missing))
        if cfg['error']:lines.append('Последняя ошибка: '+discord.utils.escape_markdown(cfg['error']))
        try:
            rows=await self.channels(i.guild);valid=[c for c,r in rows if not r]
            lines.append(f'Подходящих каналов: {len(valid)} из {len(rows)}.')
            lines.extend(f"• #{c.name}: {reason or 'подходит'}" for c,reason in rows[:15])
            if len(rows)>15:lines.append('Показаны первые 15 каналов.')
        except Exception as exc:lines.append('Не удалось получить каналы Discord: '+str(exc)[:400])
        await i.followup.send(embed=discord.Embed(title='Проверка случайных автопостов',description='\n'.join(lines)[:4000]),ephemeral=True)

    def install(self):
        @self.bot.tree.command(name='автопосты',description='Случайные события с картинками в открытых каналах: включение и интервал')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        async def settings(i:discord.Interaction,включить:bool,минут_от:app_commands.Range[int,10,1440]=60,минут_до:app_commands.Range[int,10,1440]=120):
            await i.response.defer(ephemeral=True)
            if not self.game.settings(i.guild_id):raise GameError('Сначала /настройка — выбери игровой канал.')
            self.configure(i.guild_id,включить,минут_от,минут_до)
            await i.followup.send('Включено. Первая попытка через минуту, далее случайный интервал. Проверка: /автопост_статус. Отправить сейчас: /автопост_сейчас.' if включить else 'Случайные автопосты выключены.',ephemeral=True)
        @self.bot.tree.command(name='автопост_статус',description='Проверить расписание, права каналов и последнюю ошибку автопостов')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        async def status(i:discord.Interaction):await self.status(i)
        @self.bot.tree.command(name='автопост_сейчас',description='Отправить настоящее случайное событие сейчас и проверить канал')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        async def now(i:discord.Interaction):
            await i.response.defer(ephemeral=True)
            if not self.game.settings(i.guild_id):raise GameError('Сначала /настройка.')
            await i.followup.send(await self.send(i.guild,force=True),ephemeral=True)
