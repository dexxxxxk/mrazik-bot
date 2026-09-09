"""Discord adapter for durable events, XP roles and command catalogue."""
import asyncio
import logging
import random
from datetime import datetime, time, timedelta
import discord
from discord import app_commands
from discord.ext import tasks
from content import RANKS
from logovo_content import LOGOVO_STORIES
from game import GameError

log = logging.getLogger('mrazik.features')
ADMIN = {'настройка','панель','заявки','проверить','роли_настроить','автороли','события','событие'}
GAMES = {'викторина','дуэль','экспедиция','предсказание','сундуки','угадай','рыбалка'}


class TextDestination(app_commands.Transformer):
    @property
    def type(self): return discord.AppCommandOptionType.channel

    @property
    def channel_types(self): return [discord.ChannelType.text,discord.ChannelType.news]

    async def transform(self,interaction,value):
        # Keep Discord's partial channel; resolve via HTTP after deferring the response.
        return value


async def resolve_text_channel(client,guild_id,channel_id):
    channel=client.get_channel(channel_id)
    if channel is None:
        try: channel=await client.fetch_channel(channel_id)
        except discord.Forbidden as exc:
            raise GameError('Мразик не видит выбранный канал. Разреши ему «Просматривать канал» в правах этого канала.') from exc
        except discord.NotFound as exc:
            raise GameError('Канал не найден. Выбери существующий текстовый канал.') from exc
        except discord.HTTPException as exc:
            raise GameError('Discord не ответил при загрузке канала. Попробуй ещё раз чуть позже.') from exc
    if not isinstance(channel,discord.TextChannel):
        raise GameError('Нужен обычный текстовый канал, а не голосовой канал, форум или ветка.')
    if channel.guild.id!=guild_id:
        raise GameError('Выбери канал этого сервера.')
    return channel


class CommandSelect(discord.ui.Select):
    def __init__(self, service):
        self.service = service
        super().__init__(placeholder='Раздел справки', options=[
            discord.SelectOption(label=label, value=value) for value,label in
            [('start','Профиль и питомец'),('games','Все игры'),('collection','Образы и ранги'),
             ('social','Задания и компания'),('admin','Настройки для модераторов')]])

    async def callback(self, i):
        await i.response.edit_message(embed=self.service.help_embed(self.values[0]))


class EventButton(discord.ui.DynamicItem[discord.ui.Button], template=r'mrazik:event:(?P<eid>[0-9a-f]{32}):(?P<choice>[0-4])'):
    def __init__(self,eid,choice,label='Играть',disabled=False):
        self.eid,self.choice=eid,choice
        super().__init__(discord.ui.Button(label=label,custom_id=f'mrazik:event:{eid}:{choice}',
                                          style=discord.ButtonStyle.primary,disabled=disabled))

    @classmethod
    async def from_custom_id(cls,interaction,item,match):
        return cls(match['eid'],int(match['choice']),item.label or 'Играть')

    async def callback(self,i):
        service=i.client.features
        try:
            if not await service.allowed(i): return
            await i.response.defer(ephemeral=True)
            r,correct,phrase=service.game.answer_activity(i.guild_id,i.user.id,i.channel_id,i.message.id,self.eid,self.choice)
            e=service.game.event(self.eid)
            if e['owner'] and e['kind']=='quiz':
                p=e['payload']; phrase+=f"\nВерный ответ: **{p['options'][p['answer']]}**."
            if e['owner']:
                try: await i.edit_original_response(view=service.event_view(e,True))
                except discord.HTTPException: pass
            if correct:
                phrase+=service.limit_notice(i.guild_id,i.user.id,r)
            await i.followup.send(f'{phrase}\n+{r[0]} монеток · +{r[1]} опыта',ephemeral=True)
        except Exception as error:
            await service.respond_error(i,error)


class FeatureService:
    def __init__(self,bot,game,card,allowed,respond_error):
        self.bot,self.game,self.card=bot,game,card
        self.allowed,self.respond_error=allowed,respond_error
        self.setup_locks={}
        self.role_locks={}

    def daily_limits(self,g,u):
        day=self.game.day()
        row=self.game.db.execute('SELECT coins,xp FROM earnings WHERE guild=? AND uid=? AND day=?',(g,u,day)).fetchone()
        coins,xp=tuple(row) if row else (0,0)
        tomorrow=datetime.fromisoformat(day).date()+timedelta(days=1)
        reset=datetime.combine(tomorrow,time.min,tzinfo=self.game.tz).timestamp()
        return dict(day=day,coins=coins,xp=xp,coins_left=max(0,300-coins),xp_left=max(0,200-xp),reset=int(reset))

    def limit_notice(self,g,u,reward):
        limits=self.daily_limits(g,u)
        exhausted=[]
        if not limits['coins_left']: exhausted.append('монет (300/300)')
        if not limits['xp_left']: exhausted.append('опыта (200/200)')
        if not exhausted:return ''
        return ('\nДостигнут дневной игровой лимит '+', '.join(exhausted)+
                f". Он личный. Обновление: <t:{limits['reset']}:R>. Подробнее: /награды.")

    def rewards_embed(self,g,u):
        limits=self.daily_limits(g,u)
        user=self.game.db.execute('SELECT coins,xp FROM users WHERE guild=? AND uid=?',(g,u)).fetchone()
        coins,xp=tuple(user) if user else (0,0)
        embed=discord.Embed(title='Твои награды и лимиты',color=0x9BA95B,
            description=f"Баланс: **{coins}** монет · опыт питомца: **{xp}**.\n"
            f"Игры за {limits['day']} ({self.game.tz}):\n"
            f"Монеты: {limits['coins']}/300 · осталось **{limits['coins_left']}**.\n"
            f"Опыт: {limits['xp']}/200 · осталось **{limits['xp_left']}**.\n"
            f"Лимиты обновятся <t:{limits['reset']}:R>. Покупки не восстанавливают лимит заработка.\n\n"
            'Ежедневная подачка и одобренные творческие работы — сверх лимита. '
            'Открытие /задание и отправка /сдать ещё не дают награду: нужно одобрение другого модератора через /проверить.')
        rows=self.game.db.execute('SELECT id,day,status FROM submissions WHERE guild=? AND uid=? ORDER BY id DESC LIMIT 5',(g,u)).fetchall()
        status={'pending':'ожидает проверки — пока без награды','approved':'одобрена — начислено 80 монет и 40 XP','rejected':'отклонена — без награды'}
        body='\n'.join(f"№{r['id']} · {r['day']}: {status.get(r['status'],r['status'])}" for r in rows) or 'Сданных творческих работ пока нет.'
        embed.add_field(name='Твои последние работы',value=body,inline=False)
        return embed

    def help_embed(self,category='start'):
        collection={'магазин','гардероб','одеть','ранги','альбом','роль'}
        social={'участие','кто','задание','сдать','топ','байка','награды'}
        groups={'games':GAMES,'collection':collection,'social':social,'admin':ADMIN}
        names=groups.get(category)
        commands=self.bot.tree.get_commands()
        if names is None: commands=[c for c in commands if c.name not in ADMIN|GAMES|collection|social]
        else: commands=[c for c in commands if c.name in names]
        embed=discord.Embed(title='📖 Все команды Мразика',color=0x9BA95B,
            description='Выбери раздел в меню ниже. Все монетки игровые.\n'
                        'Игровой лимит за день: 300 монеток и 200 опыта. Подачка и одобренное задание — отдельно.')
        for cmd in commands:
            params=' '.join(f'<{p.display_name}>' if p.required else f'[{p.display_name}]' for p in cmd.parameters)
            embed.add_field(name=f'/{cmd.name} {params}'.strip(),value=cmd.description,inline=False)
        embed.set_footer(text='У каждого свой Мразик • Опыт питомца определяет роль владельца')
        return embed

    async def help_response(self,i,category='start'):
        view=discord.ui.View(timeout=180)
        view.add_item(CommandSelect(self))
        await i.response.send_message(embed=self.help_embed(category),view=view,ephemeral=True)

    def event_view(self,e,closed=False):
        view=discord.ui.View(timeout=None)
        for n,label in enumerate(e['payload']['options']):
            view.add_item(EventButton(e['id'],n,label,closed))
        return view

    def event_card(self,e):
        p=e['payload']
        text=p['text']+f"\n\nКонец: <t:{int(e['expires'])}:R>. Одна попытка на участника."
        if e['kind']=='quiz':
            text+=('\nБез повторов, до 3 вопросов в день. Открытие уже расходует вопрос; пауза 10 минут.'
                   if e['owner'] else '\nЕсли ты уже открывал этот вопрос лично, повторной награды нет.')
        if e['kind']=='chests':
            amounts=', '.join(str(n) for n in sorted(p['amounts']))
            text+=f"\nВнутри {amounts} монеток на выбор и {p['xp']} опыта."
        elif e['kind']=='fish':
            text+=f"\n🎣 Подсекать: <t:{int(e['starts'])}:T> (<t:{int(e['starts'])}:R>)."
            text+=f"\nЗа улов: 10–50 монеток и {p['xp']} опыта."
        else:
            text+=f"\nНаграда: {p['coins']} монеток и {p['xp']} опыта."
        text+='\nНаграды учитывают дневной игровой лимит.'
        return self.card(p['title'],text,p['art'])

    async def publish(self,channel,e):
        try:
            message=await channel.send(**self.event_card(e),view=self.event_view(e))
            self.game.open_event(e['id'],message.id)
            return message
        except Exception:
            self.game.fail_event(e['id'])
            raise

    async def personal(self,i,kind):
        await i.response.defer(ephemeral=True)
        e=self.game.start_activity(i.guild_id,i.user.id,i.channel_id,kind)
        try:
            message=await i.followup.send(**self.event_card(e),view=self.event_view(e),ephemeral=True,wait=True)
            self.game.open_event(e['id'],message.id)
        except Exception:
            self.game.fail_event(e['id'])
            raise

    async def setup_roles(self,guild):
        lock=self.setup_locks.setdefault(guild.id,asyncio.Lock())
        async with lock:
            if not guild.me.guild_permissions.manage_roles:
                raise GameError('Выдай Мразику право «Управлять ролями» и подними его роль выше рангов.')
            role_map=self.game.rank_role_map(guild.id)
            for threshold,name in RANKS:
                role=guild.get_role(role_map.get(threshold,0))
                if role:
                    self.check_role(guild,role)
                else:
                    role=await guild.create_role(name=name,permissions=discord.Permissions.none(),
                        colour=discord.Colour(0x9BA95B),mentionable=False,reason='Игровой ранг Мразика')
                    self.game.remember_role(guild.id,threshold,role.id)
                    self.check_role(guild,role)
            self.game.enable_roles(guild.id,True)

    @staticmethod
    def check_role(guild,role):
        if role.is_default() or role.managed or role.permissions.value or role>=guild.me.top_role:
            raise GameError(f'Роль «{role.name}» должна быть без серверных прав и ниже роли Мразика.')

    async def sync_role(self,guild,uid):
        lock=self.role_locks.setdefault((guild.id,uid),asyncio.Lock())
        async with lock:
            if not self.game.roles_enabled(guild.id): return 'Автороли выключены. Модератор может включить /автороли.'
            mapping=self.game.rank_role_map(guild.id)
            if set(mapping)!=set(dict(RANKS)): raise GameError('Сначала модератор должен выполнить /роли_настроить.')
            if not guild.me.guild_permissions.manage_roles: raise GameError('Мразику нужно право «Управлять ролями».')
            # Fetch fresh roles: never trust a cached membership after an earlier update.
            member=await guild.fetch_member(uid)
            xp=self.game.user(guild.id,uid)['xp']
            threshold=max(t for t,_ in RANKS if t<=xp)
            roles={t:guild.get_role(rid) for t,rid in mapping.items()}
            if any(r is None for r in roles.values()): raise GameError('Ранг удалён. Повтори /роли_настроить.')
            for role in roles.values(): self.check_role(guild,role)
            desired=roles[threshold]
            if desired not in member.roles:
                await member.add_roles(desired,reason='Опыт личного питомца Мразика',atomic=True)
            obsolete=[r for r in member.roles if r.id in mapping.values() and r.id!=desired.id]
            if obsolete: await member.remove_roles(*obsolete,reason='Обновление игрового ранга',atomic=True)
            self.game.role_ack(guild.id,uid,xp)
            return f'Твой ранг: **{desired.name}** · {xp} опыта.'

    @tasks.loop(seconds=30)
    async def worker(self):
        try: await self.tick()
        except Exception: log.exception('Mrazik background tick failed; retry on next tick')

    @worker.before_loop
    async def before_worker(self): await self.bot.wait_until_ready()

    async def tick(self):
        self.game.expire_events()
        for cfg in self.game.db.execute('SELECT * FROM settings').fetchall():
            try: channel=await resolve_text_channel(self.bot,cfg['guild'],cfg['channel'])
            except GameError:
                log.warning('Game channel unavailable in guild %s',cfg['guild'])
                continue
            e=self.game.claim_scheduled_event(cfg['guild'],cfg['channel'])
            if e:
                try: await self.publish(channel,e)
                except discord.HTTPException: log.warning('Event delivery failed in guild %s',cfg['guild'])
        closed=self.game.db.execute("SELECT id FROM events WHERE state='closed' AND owner=0 AND summary_done=0 LIMIT 10").fetchall()
        for row in closed:
            e=self.game.event(row['id']); channel=self.bot.get_channel(e['channel'])
            if channel and e['message']:
                try:
                    total,correct=self.game.event_stats(e['id']); p=e['payload']
                    text=f'Событие завершено. Участников: {total}. Успешных попыток: {correct}.'
                    if e['kind'] in ('quiz','target'): text+=f"\nВерный ответ: **{p['options'][p['answer']]}**."
                    embed=discord.Embed(title=p['title'],description=text,color=0x9BA95B)
                    await channel.get_partial_message(e['message']).edit(embed=embed,view=self.event_view(e,True))
                except discord.NotFound: pass
                except discord.Forbidden: log.warning('Cannot close event message %s',e['id'])
                except discord.HTTPException: continue
            self.game.db.execute('UPDATE events SET summary_done=1 WHERE id=?',(e['id'],))
        queue=self.game.db.execute('''SELECT q.* FROM role_queue q JOIN role_settings s ON s.guild=q.guild
            WHERE s.enabled=1 AND q.retry_at<=? ORDER BY q.retry_at LIMIT 10''',(self.game.clock(),)).fetchall()
        for row in queue:
            guild=self.bot.get_guild(row['guild'])
            if not guild: continue
            try: await self.sync_role(guild,row['uid'])
            except discord.NotFound: self.game.role_ack(row['guild'],row['uid'],row['xp'])
            except (discord.HTTPException,GameError):
                self.game.db.execute('UPDATE role_queue SET retry_at=? WHERE guild=? AND uid=?',
                    (self.game.clock()+300,row['guild'],row['uid']))
                log.warning('Rank sync deferred for guild %s user %s; check role permissions',row['guild'],row['uid'])

    def install(self):
        tree=self.bot.tree

        @tree.command(name='награды',description='Мои дневные лимиты монет и опыта, время обновления и статусы заданий')
        @app_commands.guild_only()
        async def rewards(i:discord.Interaction):
            await i.response.defer(ephemeral=True)
            await i.followup.send(embed=self.rewards_embed(i.guild_id,i.user.id),ephemeral=True)

        @tree.command(name='байка',description='Выдуманная история про Dex, Kadi, RobedBroom и Мразика; без наград')
        @app_commands.guild_only()
        async def story(i:discord.Interaction):
            self.game.cooldown(i.guild_id,i.user.id,'story',60)
            await i.response.send_message(**self.card('Байка Мразика • выдумка',random.choice(LOGOVO_STORIES),'laugh'),ephemeral=True)

        @tree.command(name='сундуки',description='Выбрать один из трёх сундуков: бесплатные монетки и опыт, раз в 5 минут')
        @app_commands.guild_only()
        async def chests(i:discord.Interaction): await self.personal(i,'chests')

        @tree.command(name='угадай',description='Угадать число от 1 до 5: 45 монеток и 20 опыта, раз в 2 минуты')
        @app_commands.guild_only()
        async def guess(i:discord.Interaction): await self.personal(i,'guess')

        @tree.command(name='рыбалка',description='Подсечь вовремя: 10–50 монеток и 15 опыта, раз в 2 минуты')
        @app_commands.guild_only()
        async def fish(i:discord.Interaction): await self.personal(i,'fish')

        @tree.command(name='роль',description='Посмотреть и обновить свою Discord-роль по опыту своего питомца')
        @app_commands.guild_only()
        async def role(i:discord.Interaction):
            await i.response.defer(ephemeral=True)
            self.game.queue_role(i.guild_id,i.user.id)
            await i.followup.send(await self.sync_role(i.guild,i.user.id),ephemeral=True)

        @tree.command(name='роли_настроить',description='Создать шесть ролей за опыт и включить автоматическую выдачу')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        async def setup_roles(i:discord.Interaction):
            await i.response.defer(ephemeral=True)
            await self.setup_roles(i.guild)
            await i.followup.send('Роли готовы! Существующий опыт учтён. Выдача идёт очередью, обычно до минуты.\n'
                'Роль Мразика должна оставаться выше всех шести рангов.',ephemeral=True)

        @tree.command(name='автороли',description='Включить или приостановить выдачу ролей за опыт')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        async def auto_roles(i:discord.Interaction,включить:bool):
            if включить and len(self.game.rank_role_map(i.guild_id))!=len(RANKS):
                raise GameError('Сначала выполни /роли_настроить.')
            self.game.enable_roles(i.guild_id,включить)
            await i.response.send_message('Автороли включены.' if включить else 'Автороли приостановлены. Уже выданные роли сохранены.',ephemeral=True)

        @tree.command(name='события',description='Включить часовые события и задать тихие часы; одинаковые часы — без тишины')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        async def events(i:discord.Interaction,включить:bool,тихо_с:app_commands.Range[int,0,23]=0,тихо_до:app_commands.Range[int,0,23]=0):
            if not self.game.settings(i.guild_id): raise GameError('Сначала выбери игровой канал через /настройка.')
            self.game.configure_events(i.guild_id,включить,тихо_с,тихо_до)
            cfg=self.game.ensure_event_settings(i.guild_id)
            await i.response.send_message(('Часовые события включены.' if включить else 'Часовые события выключены.')+
                f'\nТихие часы: {тихо_с}:00–{тихо_до}:00 ({self.game.tz}); одинаковые — без тишины.'+
                (f"\nСледующая проверка: <t:{int(cfg['next_at'])}:R>. Во время тишины событие пропускается." if включить else ''),ephemeral=True)

        @tree.command(name='событие',description='Запустить общее событие сейчас, не чаще раза в 10 минут')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        @app_commands.choices(тип=[app_commands.Choice(name=n,value=v) for n,v in [('Случайное','random'),('Викторина','quiz'),('Заначка','stash'),('Поймай пакость','target')]])
        async def event(i:discord.Interaction,тип:app_commands.Choice[str]|None=None):
            kind=тип.value if тип else 'random'
            if kind=='random': kind=random.choice(['stash','target']+(['quiz'] if self.game.available_questions(i.guild_id) else []))
            await i.response.defer(ephemeral=True)
            e=self.game.start_activity(i.guild_id,i.user.id,i.channel_id,kind,public=True)
            await self.publish(i.channel,e)
            await i.followup.send('Мразик начал событие. Участникам доступно 5 минут.',ephemeral=True)
