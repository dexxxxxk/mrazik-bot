"""Discord interface for a daily boss, with durable hourly attacks."""
import asyncio
import logging
import discord
from discord import app_commands
from discord.ext import tasks
from boss_engine import BossStore
from game import GameError
from features import resolve_text_channel
from expansion import NavButton

log=logging.getLogger('mrazik.boss')

class BossButton(discord.ui.DynamicItem[discord.ui.Button],template=r'mrazik:boss:(?P<bid>[0-9a-f]{32}):(?P<action>hit|status)'):
    def __init__(self,bid,action='hit',disabled=False):
        self.bid,self.action=bid,action
        super().__init__(discord.ui.Button(label='⚔ Ударить босса' if action=='hit' else '📊 Мой вклад',
            style=discord.ButtonStyle.danger if action=='hit' else discord.ButtonStyle.secondary,
            custom_id=f'mrazik:boss:{bid}:{action}',disabled=disabled))
    @classmethod
    async def from_custom_id(cls,i,item,match):return cls(match['bid'],match['action'])
    async def callback(self,i):
        service=i.client.boss
        try:
            if not await i.client.features.allowed(i):return
            await i.response.defer(ephemeral=True,thinking=True)
            if self.action=='status':return await service.status_after_ack(i,self.bid)
            async with service.lock(i.guild_id):
                damage,b=service.store.hit(i.guild_id,i.user.id,i.channel_id,i.message.id,self.bid)
                # A Discord edit failure must never roll back or repeat a paid hit.
                try:await service.update_message(b)
                except (discord.HTTPException,GameError):log.exception('Boss hit saved; card update will retry')
                text=f"Ты нанёс **{damage} урона**. У босса **{b['hp']}/{b['max_hp']} HP**."
                if b['state']=='defeated':text+='\nБосс побеждён! Всем участникам уже начислены монеты и опыт. Подробности — «Мой вклад» или /босс.'
                else:text+=f"\nСледующий удар <t:{int(service.game.clock()+3600)}:R>. Награда будет после победы."
                await i.followup.send(text,ephemeral=True)
        except Exception as exc:await i.client.features.respond_error(i,exc)

class BossService:
    def __init__(self,bot,game):
        self.bot,self.game=bot,game;self.store=BossStore(game);self.locks={}
    def lock(self,g):return self.locks.setdefault(g,asyncio.Lock())

    def card(self,b):
        members=self.store.members(b['id']);count=len(members)
        filled=max(0,min(10,(b['hp']*10+b['max_hp']-1)//b['max_hp']))
        text=f"{'🟩'*filled}{'⬛'*(10-filled)}\n**{b['hp']}/{b['max_hp']} HP** · Участников: **{count}**\n"
        if b['state'] in ('open','pending'):
            text+=f"Бой до <t:{int(b['expires'])}:f>. Каждый игрок бьёт раз в час: 80–140 урона.\n"
            text+='Победите вместе: каждому участнику 100 монет и 60 XP плюс доля от 300 монет и 180 XP пропорционально урону. Последний удар не даёт особого бонуса.'
        elif b['state']=='defeated':text+='**Победа!** Награды всем участникам начислены автоматически. Своя награда — «Мой вклад». Карточка исчезнет через час после победы.'
        else:text+='Босс сбежал в полночь. За незавершённый бой награды нет. Следующая попытка — с новым боссом.'
        if members:
            text+='\n\n**Больше всего урона:**\n'+'\n'.join(f"<@{m['uid']}> — {m['damage']} урона, {m['hits']} ударов" for m in members[:5])
        return self.bot.features.card('Босс дня • '+b['name'],text,b['art'])

    def view(self,b):
        v=discord.ui.View(timeout=None)
        v.add_item(BossButton(b['id'],disabled=b['state'] not in ('open','pending')))
        v.add_item(BossButton(b['id'],'status'));v.add_item(NavButton());return v

    async def update_message(self,b):
        if not b['message']:return
        channel=await resolve_text_channel(self.bot,b['guild'],b['channel'])
        kwargs=self.card(b);attachment=kwargs.pop('file',None)
        await channel.get_partial_message(b['message']).edit(**kwargs,attachments=[attachment] if attachment else [],view=self.view(b))
        self.game.db.execute('UPDATE boss_runs SET dirty=0 WHERE id=?',(b['id'],))

    async def publish(self,b):
        cfg=self.game.settings(b['guild'])
        if not cfg:raise GameError('Игровой канал не настроен.')
        if cfg['channel']!=b['channel']:
            if b['message']:
                try:
                    old=await resolve_text_channel(self.bot,b['guild'],b['channel'])
                    await old.get_partial_message(b['message']).delete()
                except (discord.HTTPException,GameError):log.warning('Old boss card could not be removed')
            self.game.db.execute('UPDATE boss_runs SET channel=?,message=0,dirty=1 WHERE id=?',(cfg['channel'],b['id']))
            b=self.store.get(b['id'])
        channel=await resolve_text_channel(self.bot,b['guild'],b['channel'])
        if not b['message']:
            message=await channel.send(**self.card(b),view=self.view(b))
            self.game.db.execute("UPDATE boss_runs SET message=?,state='open',dirty=0 WHERE id=? AND state IN ('pending','open')",(message.id,b['id']))
            log.info('Boss published guild=%s boss=%s channel=%s',b['guild'],b['id'],b['channel'])
        elif b['dirty']:
            try:await self.update_message(b)
            except discord.NotFound:
                self.game.db.execute('UPDATE boss_runs SET message=0 WHERE id=?',(b['id'],))
                # Same persisted boss will be reposted on the next tick; HP never resets.

    async def status(self,i):
        await i.response.defer(ephemeral=True)
        await self.status_after_ack(i)

    async def status_after_ack(self,i,bid=None):
        self.store.expire();cfg=self.store.settings(i.guild_id)
        b=self.store.get(bid) if bid else self.store.today(i.guild_id)
        if b and b['guild']!=i.guild_id:raise GameError('Босс другого сервера.')
        text=f"Ежедневное появление: **{cfg['hour']:02}:00 ({self.game.tz})**. Здоровье новых боссов: {cfg['health']}.\n"
        text+='Расписание включено.\n' if cfg['enabled'] else 'Новые ежедневные боссы выключены.\n'
        if b:
            text+=f"{b['name']}: {b['hp']}/{b['max_hp']} HP. Статус: "+{'pending':'ожидает публикации','open':'идёт бой','defeated':'побеждён','escaped':'сбежал'}[b['state']]+'.\n'
            mine=next((m for m in self.store.members(b['id']) if m['uid']==i.user.id),None)
            if mine:
                text+=f"Твой вклад: {mine['damage']} урона, {mine['hits']} ударов.\n"
                if b['state']=='defeated':text+=f"Получено: **{mine['coins']} монет и {mine['xp']} XP**.\n"
                elif b['state']=='open':text+=f"Следующий удар <t:{int(mine['last_hit']+3600)}:R>.\n"
            else:text+='Ты пока не участвовал в этом бою.\n'
        elif cfg['enabled']:text+=f"Следующее появление <t:{int(self.store.next_spawn(i.guild_id))}:R>.\n"
        if cfg['error']:text+='Последняя ошибка публикации: '+discord.utils.escape_markdown(cfg['error'])+'\n'
        v=discord.ui.View(timeout=None);v.add_item(NavButton())
        if b and b['message'] and not b['cleaned']:
            v.add_item(discord.ui.Button(label='Открыть бой',url=f"https://discord.com/channels/{b['guild']}/{b['channel']}/{b['message']}"))
        await i.followup.send(embed=discord.Embed(title='👹 Босс Логова',description=text,color=0xAA4433),view=v,ephemeral=True)

    async def tick_guild(self,g,force=False):
        async with self.lock(g):
            b=self.store.spawn(g,force)
            if not b or b['state'] not in ('pending','open'):return b
            if b['retry_at']>self.game.clock() and not force:return b
            if force:
                self.game.db.execute('UPDATE boss_runs SET dirty=1 WHERE id=?',(b['id'],))
                b=self.store.get(b['id'])
            self.game.db.execute('UPDATE boss_runs SET retry_at=? WHERE id=?',(self.game.clock()+300,b['id']))
            try:
                await self.publish(b)
                self.game.db.execute('UPDATE boss_runs SET retry_at=0 WHERE id=?',(b['id'],))
                self.game.db.execute("UPDATE boss_settings SET error='' WHERE guild=?",(g,))
            except Exception as exc:
                self.game.db.execute('UPDATE boss_settings SET error=? WHERE guild=?',(str(exc)[:500],g))
                log.exception('Boss delivery failed guild=%s; retry in five minutes',g)
                if force:raise GameError('Босс сохранён, но карточка не отправлена. /босс покажет ошибку.') from exc
            return self.store.get(b['id'])

    async def tick(self):
        self.store.expire()
        for row in self.game.db.execute('SELECT guild FROM settings').fetchall():
            try:await self.tick_guild(row['guild'])
            except Exception:log.exception('Boss scheduler failed guild=%s',row['guild'])
        rows=self.game.db.execute("SELECT id FROM boss_runs WHERE state IN ('defeated','escaped') AND cleaned=0 ORDER BY created LIMIT 10").fetchall()
        for row in rows:
            b=self.store.get(row['id'])
            try:
                if b['message']:
                    if b['cleanup_at']<=self.game.clock():
                        channel=await resolve_text_channel(self.bot,b['guild'],b['channel'])
                        await channel.get_partial_message(b['message']).delete()
                    elif b['dirty']:await self.update_message(b)
                if not b['message'] or b['cleanup_at']<=self.game.clock():self.game.db.execute('UPDATE boss_runs SET cleaned=1 WHERE id=?',(b['id'],))
            except discord.NotFound:self.game.db.execute('UPDATE boss_runs SET cleaned=1 WHERE id=?',(b['id'],))
            except (discord.HTTPException,GameError):log.warning('Boss cleanup deferred id=%s',b['id'])

    @tasks.loop(seconds=30)
    async def worker(self):
        try:await self.tick()
        except Exception:log.exception('Boss worker tick failed')
    @worker.before_loop
    async def before_worker(self):await self.bot.wait_until_ready()

    def install(self):
        @self.bot.tree.command(name='босс',description='Босс дня: здоровье, мой вклад, награда и время следующего удара')
        @app_commands.guild_only()
        async def status(i:discord.Interaction):await self.status(i)
        @self.bot.tree.command(name='босс_настройка',description='Настроить ежедневного босса: включение, час появления и здоровье новых боссов')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        async def settings(i:discord.Interaction,включить:bool,час:app_commands.Range[int,0,23]=12,здоровье:app_commands.Range[int,300,100000]=1500):
            await i.response.defer(ephemeral=True)
            if not self.game.settings(i.guild_id):raise GameError('Сначала /настройка — выбери игровой канал.')
            self.store.configure(i.guild_id,включить,час,здоровье)
            await i.followup.send(f"Новые боссы {'включены' if включить else 'выключены'}: {час:02}:00 ({self.game.tz}), {здоровье} HP. Текущий бой не меняется. До полуночи нужно победить. Проверка: /босс; ранний старт сегодняшнего боя: /босс_призвать.",ephemeral=True)
        @self.bot.tree.command(name='босс_призвать',description='Начать бой с боссом сегодня раньше расписания, максимум один босс за день')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        async def summon(i:discord.Interaction):
            await i.response.defer(ephemeral=True)
            await self.tick_guild(i.guild_id,force=True)
            await self.status_after_ack(i)
