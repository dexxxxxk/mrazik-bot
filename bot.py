"""Мразик: развлечения для Мразотного Логова. Python 3.11+."""
import asyncio
import logging
import os
import random
import time
import uuid
from pathlib import Path
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from content import OUTFITS, RANKS, QUIZ, FORTUNES, badges, rank
from game import Game, GameError
from features import FeatureService, EventButton, resolve_text_channel, TextDestination
from expansion import Expansion, NavButton, OpenChest, MazeMove
from roaming import Roaming

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
DB_PATH = Path(os.getenv('DATABASE_PATH', 'data/gnid.sqlite3'))
game = Game(str(DB_PATH if DB_PATH.is_absolute() else ROOT / DB_PATH), os.getenv('TZ','Europe/Moscow'))
log = logging.getLogger('gnid')


def reward_text(reward):
    return f'+{reward[0]} монеток · +{reward[1]} опыта'


def card(title, description, art='base'):
    embed = discord.Embed(title=title, description=description, color=0x9BA95B)
    embed.set_footer(text='Мразотное Логово • Мразик всё записывает')
    path = ROOT / 'assets' / f'{art}.png'
    kwargs = {'embed':embed}
    if path.is_file():
        embed.set_image(url=f'attachment://{art}.png')
        kwargs['file'] = discord.File(path, filename=f'{art}.png')
    return kwargs


async def respond_error(i, error):
    error = getattr(error, 'original', error)
    if isinstance(error, discord.HTTPException) and error.code in (10062,40060,10015):
        log.warning('Interaction unavailable: command=%s code=%s; no retry',getattr(i.command,'name','button'),error.code)
        return
    if isinstance(error, GameError):
        text = str(error)
    elif isinstance(error, app_commands.MissingPermissions):
        text = 'Это действие для участника с правом «Управлять сервером».'
    elif isinstance(error, app_commands.TransformerError):
        text = 'Не удалось распознать параметр команды. Выбери канал заново из списка Discord; для настройки нужен обычный текстовый канал.'
    elif isinstance(error, app_commands.CommandOnCooldown):
        text = f'Не спеши. Повтори через {error.retry_after:.0f} сек.'
    else:
        log.error('Interaction failed', exc_info=(type(error),error,error.__traceback__))
        text = 'Мразик споткнулся. Попробуй снова; если повторится — сообщи владельцу бота.'
    try:
        if i.response.is_done():
            await i.followup.send(text, ephemeral=True)
        else:
            await i.response.send_message(text, ephemeral=True)
    except discord.HTTPException as delivery_error:
        log.warning('Could not deliver private error: command=%s code=%s',getattr(i.command,'name','button'),delivery_error.code)


async def allowed(i, bypass_channel=False):
    if not i.guild_id:
        await i.response.send_message('Мразик живёт на сервере. В личке не играем.', ephemeral=True)
        return False
    if bypass_channel:
        return True
    config = game.settings(i.guild_id)
    if not bypass_channel and not config:
        await i.response.send_message('Сначала модератор должен выбрать игровой канал: /настройка.',ephemeral=True)
        return False
    if not bypass_channel and config and i.channel_id != config['channel']:
        await i.response.send_message(f"Играем в <#{config['channel']}>.",ephemeral=True)
        return False
    return True


class Tree(app_commands.CommandTree):
    async def interaction_check(self, i):
        bypass = i.command and i.command.name in {'настройка','помощь','проверить','заявки','панель','роли_настроить','автороли','события','роль','инвентарь','сундук_события','автопосты','автопост_статус','автопост_сейчас'}
        return await allowed(i, bypass)

    async def on_error(self, i, error):
        await respond_error(i,error)


class SafeView(discord.ui.View):
    def __init__(self, *, timeout=120):
        super().__init__(timeout=timeout)
        self.message = None
        self.add_item(NavButton())

    async def interaction_check(self, i):
        custom=(i.data or {}).get("custom_id", "")
        bypass=custom.startswith(("mrazik:nav:","mrazik:auto:")) or custom in {"mrazik:bag:home","mrazik:help:v2","mrazik:games:v2"}
        return await allowed(i,bypass)

    async def on_error(self, i, error, item):
        await respond_error(i,error)

    async def on_timeout(self):
        for child in self.children:
            if not isinstance(child,NavButton):child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def send_card(i, **kwargs):
    if kwargs.get('ephemeral'):
        if 'view' not in kwargs:
            view=discord.ui.View(timeout=None);view.add_item(NavButton());kwargs['view']=view
        if getattr(i,'message',None) and i.message.flags.ephemeral:
            kwargs.pop('ephemeral')
            attachment=kwargs.pop('file',None)
            return await i.response.edit_message(attachments=[attachment] if attachment else [],content=None,**kwargs)
    return await i.response.send_message(**kwargs)


async def send_view(i, view, **kwargs):
    if getattr(i,'message',None) and i.message.flags.ephemeral:
        kwargs.pop('ephemeral',None)
        attachment=kwargs.pop('file',None)
        await i.response.edit_message(view=view,attachments=[attachment] if attachment else [],**kwargs)
    else:await i.response.send_message(view=view, **kwargs)
    view.message = await i.original_response()


def profile(g,u):
    p = game.user(g,u)
    own = game.owned(g,u)
    awards = badges(p,own)
    nxt = next((x for x in RANKS if x[0]>p['xp']),None)
    body = (f"**{rank(p['xp'])}**\n🪙 {p['coins']} монеток · ✨ {p['xp']} опыта питомца\n"
            f"Серия визитов: {p['streak']} · Победы: {p['wins']} · Ответы: {p['correct']}\n"
            f"Костюм: {OUTFITS[p['outfit']]['name']}\n"
            + (f'Следующий ранг: {nxt[1]} — ещё {nxt[0]-p["xp"]} опыта.\n' if nxt else 'Высший ранг достигнут.\n')
            + ('\n'+' · '.join(awards) if awards else '\nДостижения ещё впереди.'))
    return card('Паспорт твоего Мразика',body,p['outfit'])


def pet_card(g,u):
    p=game.pet(g,u)
    art = 'hungry' if p['food']<20 else ('sleep' if p['energy']<20 else p['outfit'])
    return card('Твой Мразик • личный питомец',
        f"**{rank(p['xp'])}** · {p['xp']} опыта питомца\n"
        f"🥣 Сытость: {p['food']:.0f}/100\n🎭 Настроение: {p['mood']:.0f}/100\n⚡ Энергия: {p['energy']:.0f}/100\n"
        f"👕 {OUTFITS[p['outfit']]['name']}\n\nНе умрёт без внимания. Просто станет ещё противнее.",art)


class PetView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    async def care(self,i,action):
        r=game.care(i.guild_id,i.user.id,action)
        phrases={'feed':'Сожрал. На повара пожаловался.','play':'Поиграл. Объявил себя победителем.',
                 'sleep':'Захрапел. Логово выдохнуло.'}
        art={'feed':'chef','play':'laugh','sleep':'sleep'}[action]
        await send_card(i,**card(phrases[action],reward_text(r),art),ephemeral=True)

    @discord.ui.button(label='Покормить',emoji='🥣',custom_id='gnid:feed:v1',style=discord.ButtonStyle.success)
    async def feed(self,i,button): await self.care(i,'feed')

    @discord.ui.button(label='Поиграть',emoji='🎲',custom_id='gnid:play:v1')
    async def play(self,i,button): await self.care(i,'play')

    @discord.ui.button(label='Уложить',emoji='💤',custom_id='gnid:sleep:v1')
    async def sleep(self,i,button): await self.care(i,'sleep')

    @discord.ui.button(label='Обновить состояние',custom_id='gnid:refresh:v1',row=1)
    async def refresh(self,i,button):
        await send_card(i,**pet_card(i.guild_id,i.user.id),ephemeral=True)


class HomeView(SafeView):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label='Мой профиль',emoji='🪪',custom_id='gnid:profile:v1')
    async def me(self,i,button):
        await send_card(i,**profile(i.guild_id,i.user.id),ephemeral=True)

    @discord.ui.button(label='Подачка дня',emoji='🪙',custom_id='gnid:daily:v1',style=discord.ButtonStyle.success)
    async def daily(self,i,button): await daily_response(i)

    @discord.ui.button(label='Мой Мразик',emoji='🐾',custom_id='gnid:pet:v1')
    async def pet(self,i,button):
        await send_card(i,**pet_card(i.guild_id,i.user.id),view=PetView(),ephemeral=True)

    @discord.ui.button(label='Магазин',emoji='👕',custom_id='gnid:shop:v1',row=1)
    async def shop(self,i,button): await shop_response(i)

    @discord.ui.button(label='Викторина',emoji='🧠',custom_id='gnid:quiz:v1',row=1)
    async def quiz(self,i,button): await quiz_response(i)

    @discord.ui.button(label='Задание дня',emoji='🎯',custom_id='gnid:quest:v1',row=1)
    async def quest(self,i,button): await quest_response(i)

    @discord.ui.button(label='Инвентарь',emoji='🎒',custom_id='mrazik:bag:home',row=2)
    async def bag_button(self,i,button):await bot.expansion.navigate(i,'bag')

    @discord.ui.button(label='Лабиринт',emoji='🧩',custom_id='mrazik:maze:home',row=2)
    async def maze_button(self,i,button):await bot.expansion.navigate(i,'maze')

    @discord.ui.button(label='Все команды',emoji='📖',custom_id='mrazik:help:v2',row=2)
    async def help_button(self,i,button): await bot.features.help_response(i)

    @discord.ui.button(label='Больше игр',emoji='🎮',custom_id='mrazik:games:v2',row=2)
    async def games_button(self,i,button): await bot.expansion.navigate(i,'games')


    @discord.ui.button(label='Автопосты: проверка',custom_id='mrazik:auto:status',row=3)
    async def autopost_status(self,i,button):await autopost_control(i,'status')

    @discord.ui.button(label='Включить автопосты',custom_id='mrazik:auto:enable',row=3)
    async def autopost_enable(self,i,button):await autopost_control(i,'enable')

    @discord.ui.button(label='Событие сейчас',custom_id='mrazik:auto:now',row=3)
    async def autopost_now(self,i,button):await autopost_control(i,'now')


async def autopost_control(i,action):
    if not i.guild or not getattr(getattr(i.user,'guild_permissions',None),'manage_guild',False):
        return await i.response.send_message('Нужно право «Управлять сервером».',ephemeral=True)
    if action=='status':return await bot.roaming.status(i)
    await i.response.defer(ephemeral=True)
    if not game.settings(i.guild_id):raise GameError('Сначала выбери игровой канал через /настройка.')
    if action=='enable':
        bot.roaming.configure(i.guild_id,True,60,120)
        text='Автопосты включены: первая попытка через минуту, затем раз в 60–120 минут. Можно нажать «Событие сейчас».'
    else:text=await bot.roaming.send(i.guild,force=True)
    await i.followup.send(text,ephemeral=True)


class OutfitSelect(discord.ui.Select):
    def __init__(self, owner, mode, items, page=1):
        self.owner,self.mode=owner,mode
        options=[discord.SelectOption(label=OUTFITS[k]['name'],value=k,
                   description=("Редкий образ из сундука" if OUTFITS[k].get("rare") else f"{OUTFITS[k]['price']} монеток • от {OUTFITS[k]['xp']} опыта")) for k in items]
        super().__init__(placeholder=f'Выбери образ • меню {page}',options=options)

    async def callback(self,i):
        if i.user.id!=self.owner:
            return await i.response.send_message('Открой свой магазин или гардероб.',ephemeral=True)
        key=self.values[0]
        if self.mode=='buy':
            game.purchase(i.guild_id,i.user.id,key)
            text='Куплено! Надеть на своего Мразика: /гардероб или /одеть.'
        else:
            game.equip(i.guild_id,i.user.id,key)
            text='Мразик нарядился. И немедленно заважничал.'
        await send_card(i,**card(OUTFITS[key]['name'],text,key),ephemeral=True)


async def shop_response(i):
    body='\n'.join(f"**{v['name']}** — {v['price']} 🪙 · от {v['xp']} XP" for k,v in OUTFITS.items() if k!='base' and not v.get('rare'))
    body+='\n\nВыбор в меню сразу покупает костюм за игровые монетки. Вещи навсегда; бонусов к победе нет.'
    view=SafeView()
    view.add_item(OutfitSelect(i.user.id,'buy',[k for k,v in OUTFITS.items() if k!='base' and not v.get('rare')]))
    await send_view(i,view,**card('Лавка подозрительного тряпья',body,'gopnik'),ephemeral=True)


async def quiz_response(i):
    await bot.features.personal(i,'quiz')


class DuelView(SafeView):
    def __init__(self,challenger,opponent):
        super().__init__(timeout=120)
        self.a,self.b=challenger,opponent
        self.accepted=False
        self.choices={}
        self.done=False
        for number,label in enumerate(['🪨 Камень','✂️ Ножницы','📄 Бумага']):
            button=discord.ui.Button(label=label,row=1,disabled=True)
            async def choose(i,n=number):
                if i.user.id not in (self.a,self.b): raise GameError('Это чужая дуэль.')
                if self.done or not self.accepted: raise GameError('Дуэль уже закрыта или ещё не принята.')
                if i.user.id in self.choices: raise GameError('Ты уже сделал тайный выбор.')
                self.choices[i.user.id]=n
                if len(self.choices)<2:
                    return await i.response.send_message('Выбор спрятан. Ждём соперника.',ephemeral=True)
                x,y=self.choices[self.a],self.choices[self.b]
                self.done=True
                labels=['камень','ножницы','бумага']
                result=f'<@{self.a}>: {labels[x]} · <@{self.b}>: {labels[y]}\n'
                if x==y:
                    result+='Ничья. Мразик присудил победу себе. Наград нет.'
                else:
                    winner,loser=(self.a,self.b) if (x-y)%3==2 else (self.b,self.a)
                    win_reward,lose_reward=game.duel_reward(i.guild_id,winner,loser)
                    result+=f'Победил <@{winner}>: {reward_text(win_reward)}\nСопернику: {reward_text(lose_reward)}'
                for child in self.children:
                    if not isinstance(child,NavButton):child.disabled=True
                await i.response.edit_message(content=result,view=self)
                self.stop()
            button.callback=choose
            self.add_item(button)

    @discord.ui.button(label='Принять вызов',style=discord.ButtonStyle.success,row=0)
    async def accept(self,i,button):
        if i.user.id!=self.b: raise GameError('Вызов адресован другому участнику.')
        if self.accepted or self.done: raise GameError('Этот вызов уже обработан.')
        game.cooldown(i.guild_id,self.b,'duel',120)
        self.accepted=True
        button.disabled=True
        for child in self.children:
            if child.row==1: child.disabled=False
        await i.response.edit_message(content='Вызов принят! Каждый выбирает кнопку. Выбор скрыт до конца раунда.',view=self)

    @discord.ui.button(label='Отказаться / отменить',row=0)
    async def cancel(self,i,button):
        if i.user.id not in (self.a,self.b): raise GameError('Это чужой вызов.')
        if self.accepted: raise GameError('Игра уже началась. Сделай выбор или дождись тайм-аута.')
        self.done=True
        for child in self.children:
            if not isinstance(child,NavButton):child.disabled=True
        await i.response.edit_message(content='Дуэль отменена. Никто ничего не потерял.',view=self)
        self.stop()

    async def on_timeout(self):
        self.done=True
        await super().on_timeout()
        if self.message:
            try: await self.message.edit(content='Время дуэли вышло. Наград нет.')
            except discord.HTTPException: pass


class Gnid(commands.Bot):
    def __init__(self):
        intents=discord.Intents.none()
        intents.guilds=True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents,
                         tree_cls=Tree,allowed_mentions=discord.AllowedMentions.none(),help_command=None)

    async def setup_hook(self):
        self.add_dynamic_items(EventButton,NavButton,OpenChest,MazeMove)
        self.add_view(HomeView())
        self.add_view(PetView())
        gid=os.getenv('GUILD_ID','').strip()
        log.info('COMMAND_SYNC_BEGIN application=%s GUILD_ID=%s local_count=%s',self.application_id,gid or '(global)',len(self.tree.get_commands()))
        if gid:
            guild=discord.Object(id=int(gid))
            self.tree.copy_global_to(guild=guild)
            synced=await self.tree.sync(guild=guild)
        else:
            synced=await self.tree.sync()
        names={c.name for c in synced}
        log.info('COMMAND_SYNC_RESULT scope=%s count=%s autoposts=%s status=%s now=%s',gid or 'global',len(synced),
            'автопосты' in names,'автопост_статус' in names,'автопост_сейчас' in names)
        for c in synced:
            if c.name.startswith('автопост'):
                log.info('COMMAND_REGISTERED name=%s id=%s default_permissions=%s',c.name,c.id,c.default_member_permissions)
        self.daily_posts.start()
        self.features.worker.start()
        self.roaming.worker.start()

    async def close(self):
        self.daily_posts.cancel()
        self.features.worker.cancel()
        self.roaming.worker.cancel()
        await super().close()
        game.db.close()

    @tasks.loop(minutes=1)
    async def daily_posts(self):
        for cfg in game.db.execute('SELECT * FROM settings WHERE automatic=1').fetchall():
            if game.now().hour<cfg['hour']: continue
            try: channel=await resolve_text_channel(self,cfg['guild'],cfg['channel'])
            except GameError:
                log.warning('Daily post channel unavailable in guild %s',cfg['guild'])
                continue
            if not game.claim_post(cfg['guild']): continue
            try:
                await channel.send(**card('Мразик принёс задание дня',
                    game.quest()+'\n\nСдать: /сдать. После одобрения модератора: 80 монеток и 40 опыта.','chef'))
            except discord.HTTPException:
                # Keep the claim: an uncertain network delivery must not duplicate a post.
                log.warning('Daily post failed for guild %s; manual /задание remains available',cfg['guild'])

    @daily_posts.before_loop
    async def before_daily(self): await self.wait_until_ready()


bot=Gnid()
bot.features=FeatureService(bot,game,card,allowed,respond_error)
bot.features.install()
bot.expansion=Expansion(bot,game,HomeView)
bot.expansion.install()
bot.roaming=Roaming(bot,game)
bot.roaming.install()


async def daily_response(i):
    r,streak=game.daily(i.guild_id,i.user.id)
    await send_card(i,**card('Держи. И не привыкай.',
        f'{reward_text(r)}\nСерия визитов: {streak} дн.','victory'),ephemeral=True)


async def quest_response(i):
    await send_card(i,**card('Задание дня',
        game.quest()+'\n\nСдай через /сдать с текстом или картинкой. Модератор проверит.\nНаграда: 80 монеток и 40 опыта.','chef'),ephemeral=True)


@bot.tree.command(name='помощь',description='Что умеет эта мразота')
@app_commands.guild_only()
async def help_command(i:discord.Interaction):
    await bot.features.help_response(i)


@bot.tree.command(name='профиль',description='Монетки, ранг, достижения и твой образ Мразика')
@app_commands.guild_only()
async def profile_command(i:discord.Interaction):
    await send_card(i,**profile(i.guild_id,i.user.id),ephemeral=True)


@bot.tree.command(name='ежедневно',description='Забрать подачку дня и продолжить серию визитов')
@app_commands.guild_only()
async def daily_command(i:discord.Interaction): await daily_response(i)


@bot.tree.command(name='мразик',description='Открыть своего Мразика: состояние, опыт, кормление и игры')
@app_commands.guild_only()
async def pet_command(i:discord.Interaction):
    await send_card(i,**pet_card(i.guild_id,i.user.id),view=PetView(),ephemeral=True)


@bot.tree.command(name='магазин',description='Купить костюм за игровые монетки')
@app_commands.guild_only()
async def shop_command(i:discord.Interaction): await shop_response(i)


@bot.tree.command(name='гардероб',description='Твоя коллекция: выбрать образ для профиля')
@app_commands.guild_only()
async def wardrobe_command(i:discord.Interaction):
    view=SafeView()
    owned=game.owned(i.guild_id,i.user.id)
    for n in range(0,len(owned),25):view.add_item(OutfitSelect(i.user.id,'personal',owned[n:n+25],n//25+1))
    await send_view(i,view,**card('Твои тряпки','\n'.join(OUTFITS[k]['name'] for k in owned)),ephemeral=True)


@bot.tree.command(name='одеть',description='Надеть купленный костюм на своего Мразика')
@app_commands.guild_only()
async def dress_command(i:discord.Interaction):
    view=SafeView()
    owned=game.owned(i.guild_id,i.user.id)
    for n in range(0,len(owned),25):view.add_item(OutfitSelect(i.user.id,'personal',owned[n:n+25],n//25+1))
    await send_view(i,view,**card('Наряди своего Мразика','Выбирай из своей коллекции. Переодеваешь только своего питомца.'),ephemeral=True)


@bot.tree.command(name='ранги',description='Ранги участников и пороги опыта')
@app_commands.guild_only()
async def ranks_command(i:discord.Interaction):
    await send_card(i,**card('Лестница сомнительного успеха',
        '\n'.join(f'**{name}** — {xp} XP' for xp,name in RANKS)+'\n\nРанг зависит от опыта твоего питомца. Discord-роли включает модератор: /роли_настроить. Костюмы покупаются отдельно.','king'),ephemeral=True)


ART_NAMES={**{k:v['name'] for k,v in OUTFITS.items()},'hungry':'Голодный Мразик',
           'sleep':'Спящая мразота','laugh':'Злорадство','victory':'Нечестная победа'}


@bot.tree.command(name='альбом',description='Альбом: 36 картинок, включая редкие образы; начни вводить название')
@app_commands.guild_only()
async def album_command(i:discord.Interaction,образ:str):
    key=образ
    if key not in ART_NAMES:raise GameError("Выбери образ из подсказок при вводе команды.")
    info=OUTFITS.get(key)
    description=(f"Цена: {info['price']} монеток · Нужно {info['xp']} опыта.\nКупить: /магазин." if info and key!='base'
                 else 'Обитатель Логова во всей своей сомнительной красе.')
    if info and info.get('rare'):description='Редкий образ: выпадает из коллекционного сундука. Купить нельзя.'
    await send_card(i,**card(ART_NAMES[key],description,key),ephemeral=True)


@album_command.autocomplete('образ')
async def album_autocomplete(i:discord.Interaction,current:str):
    return [app_commands.Choice(name=v,value=k) for k,v in ART_NAMES.items() if current.casefold() in v.casefold() or current.casefold() in k][:25]


@bot.tree.command(name='викторина',description='Новый вопрос без повторов: 45 секунд, пауза 10 минут, до 3 вопросов в день')
@app_commands.guild_only()
async def quiz_command(i:discord.Interaction): await quiz_response(i)


@bot.tree.command(name='дуэль',description='Вызвать участника на камень-ножницы-бумагу без ставок')
@app_commands.guild_only()
@app_commands.describe(соперник='Участник, который должен принять вызов')
async def duel_command(i:discord.Interaction,соперник:discord.Member):
    if соперник.bot or соперник.id==i.user.id: raise GameError('Выбери другого живого участника.')
    game.cooldown(i.guild_id,i.user.id,'duel',120)
    view=DuelView(i.user.id,соперник.id)
    await send_view(i,view,content=f'<@{i.user.id}> вызывает <@{соперник.id}>. На всё 120 секунд.',
                    **card('Дворовая дуэль','Соперник принимает вызов, затем оба тайно выбирают жест.','knight'))


@bot.tree.command(name='экспедиция',description='Отправиться с Мразиком за сомнительным добром раз в 4 часа')
@app_commands.guild_only()
async def expedition_command(i:discord.Interaction):
    event,r=game.expedition(i.guild_id,i.user.id)
    await send_card(i,**card('Вылазка на помойку',event+'\n'+reward_text(r),'hobo'),ephemeral=True)


@bot.tree.command(name='предсказание',description='Сомнительная мудрость Мразика')
@app_commands.guild_only()
async def fortune_command(i:discord.Interaction):
    game.cooldown(i.guild_id,i.user.id,'fortune',60)
    await send_card(i,**card('Мразик видит твоё будущее',random.choice(FORTUNES),'mage'),ephemeral=True)


@bot.tree.command(name='участие',description='Добровольно вступить в розыгрыш шуточных званий или выйти')
@app_commands.guild_only()
async def opt_command(i:discord.Interaction,включить:bool):
    game.opt(i.guild_id,i.user.id,включить)
    await i.response.send_message('Ты в розыгрыше званий.' if включить else 'Ты вышел из розыгрыша званий.',ephemeral=True)


@bot.tree.command(name='кто',description='Кому досталось сегодняшнее шуточное звание')
@app_commands.guild_only()
async def title_command(i:discord.Interaction):
    game.cooldown(i.guild_id,i.user.id,'title',60)
    u,title=game.title(i.guild_id)
    await send_card(i,**card(title,f'Сегодня это <@{u}>.\nУчастие добровольное: /участие.','laugh'))


@bot.tree.command(name='топ',description='Десятка участников по опыту за неделю или за всё время')
@app_commands.guild_only()
async def top_command(i:discord.Interaction,за_всё_время:bool=False):
    rows=game.top(i.guild_id,not за_всё_время)
    body='\n'.join(f'{n}. <@{r[0]}> — {r[1]} XP' for n,r in enumerate(rows,1)) or 'Пока пусто. Самое время стать первым.'
    await send_card(i,**card('Слава Логова • '+('всё время' if за_всё_время else 'эта неделя'),body,'king'),ephemeral=True)


@bot.tree.command(name='задание',description='Творческое задание дня')
@app_commands.guild_only()
async def quest_command(i:discord.Interaction): await quest_response(i)


@bot.tree.command(name='сдать',description='Отправить работу на проверку модератору; одна работа в день')
@app_commands.guild_only()
async def submit_command(i:discord.Interaction,текст:app_commands.Range[str,1,1000],картинка:discord.Attachment|None=None):
    if картинка and (not (картинка.content_type or '').startswith('image/') or картинка.size>8*1024*1024):
        raise GameError('Нужна картинка до 8 МБ.')
    await i.response.defer(ephemeral=True)
    saved=None
    try:
        if картинка:
            folder=Path(game.db.execute('PRAGMA database_list').fetchone()[2]).parent/'submissions'
            folder.mkdir(parents=True,exist_ok=True)
            extensions={'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp','image/gif':'.gif'}
            ext=extensions.get(картинка.content_type)
            if not ext: raise GameError('Принимаю PNG, JPEG, WebP или GIF.')
            saved=folder/(uuid.uuid4().hex+ext)
            await картинка.save(saved)
        sid=game.submit(i.guild_id,i.user.id,текст,saved.name if saved else '')
    except Exception:
        if saved: saved.unlink(missing_ok=True)
        raise
    await i.followup.send(f'Работа №{sid} принята. Модератор найдёт её в /заявки. Награда после проверки.',ephemeral=True)


@bot.tree.command(name='заявки',description='Посмотреть ожидающие проверки работы')
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def submissions_command(i:discord.Interaction,номер: int|None=None):
    if номер is None:
        rows=game.db.execute("SELECT id,uid,day FROM submissions WHERE guild=? AND status='pending' ORDER BY id LIMIT 20",(i.guild_id,)).fetchall()
        body='\n'.join(f"№{r['id']} · <@{r['uid']}> · {r['day']}" for r in rows) or 'Очередь пуста.'
        await i.response.send_message(body+'\nОткрыть: /заявки номер. Решение: /проверить.',ephemeral=True)
    else:
        r=game.db.execute('SELECT * FROM submissions WHERE guild=? AND id=?',(i.guild_id,номер)).fetchone()
        if not r: raise GameError('Работа не найдена.')
        embed=discord.Embed(title=f"Работа №{r['id']} • {r['status']}",
            description=f"<@{r['uid']}> · {r['day']}\n**Задание:** {game.quest(r['day'])}\n\n"+discord.utils.escape_markdown(r['body']))
        kwargs={}
        if r['url']:
            folder=Path(game.db.execute('PRAGMA database_list').fetchone()[2]).parent/'submissions'
            saved=folder/Path(r['url']).name
            if saved.is_file():
                kwargs['file']=discord.File(saved,filename=saved.name)
                embed.set_image(url=f'attachment://{saved.name}')
            else: embed.add_field(name='Вложение',value='Файл недоступен. Запросите его у автора.')
        await i.response.send_message(embed=embed,ephemeral=True,**kwargs)


@bot.tree.command(name='проверить',description='Одобрить или отклонить работу; награда выдаётся один раз')
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def review_command(i:discord.Interaction,номер:int,одобрить:bool):
    u,r=game.review(i.guild_id,i.user.id,номер,одобрить)
    await i.response.send_message(f'Работа №{номер}: '+('одобрена' if одобрить else 'отклонена')+f'. <@{u}>: {reward_text(r)}',ephemeral=True)


@bot.tree.command(name='настройка',description='Выбрать игровой канал и необязательную ежедневную публикацию')
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_command(i:discord.Interaction,канал:app_commands.Transform[app_commands.AppCommandChannel,TextDestination],час:app_commands.Range[int,0,23]=12,автопост:bool=False):
    started=time.monotonic()
    age=(discord.utils.utcnow()-i.created_at).total_seconds()
    try:
        await i.response.defer(ephemeral=True)
    except discord.HTTPException as exc:
        log.warning('Setup ACK failed: code=%s age_at_start=%.3fs request_elapsed=%.3fs gateway_latency=%.3fs',
            exc.code,age,time.monotonic()-started,i.client.latency)
        raise
    канал=await resolve_text_channel(i.client,i.guild_id,канал.id)
    if not i.guild or not i.guild.me:
        raise GameError('Мразик не найден среди участников сервера. Проверь установку приложения с ботом на сервер и перезапусти его.')
    perms=канал.permissions_for(i.guild.me)
    if not all([perms.view_channel,perms.send_messages,perms.embed_links,perms.attach_files,perms.read_message_history]):
        raise GameError('В канале нужны права: видеть канал, отправлять сообщения, вставлять ссылки, прикреплять файлы и читать историю.')
    game.configure(i.guild_id,канал.id,час,автопост)
    await i.followup.send(f'Игровой канал: {канал.mention}. Автопост: {автопост}, час {час}:00 ({game.tz}).\nТеперь отправь /панель в игровом канале.',ephemeral=True)


@bot.tree.command(name='панель',description='Опубликовать главную панель Мразика в игровом канале')
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def panel_command(i:discord.Interaction):
    if not await allowed(i): return
    await send_card(i,**card('Мразотное Логово',
        'Я Мразик. Живу тут, жру тут, осуждаю тоже тут.\n\n'
        'Прокачивай своего Мразика, собирай тряпки, вызывай друзей на дуэли. Кнопки ниже — твой вход в Логово.\n'
        'Все команды: /помощь.'),view=HomeView())


@bot.event
async def on_ready():
    ids=[g.id for g in bot.guilds]
    log.info('COMMAND_READY bot_user=%s connected_guilds=%s',bot.user.id,ids)
    target=os.getenv('GUILD_ID','').strip()
    if target and int(target) not in ids:
        log.warning('COMMAND_WRONG_GUILD configured=%s is not among connected servers',target)
    # Server nickname only: never replace the user's token or application settings.
    for guild in bot.guilds:
        if guild.me and guild.me.display_name != 'Мразик':
            try: await guild.me.edit(nick='Мразик',reason='Имя персонажа версии 2')
            except discord.HTTPException:
                log.warning('Cannot set nickname in guild %s; set Мразик manually',guild.id)


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    log.info('Mrazik build 4.2.1: command sync diagnostics + autopost panel buttons')
    token=os.getenv('DISCORD_TOKEN','').strip()
    if not token: raise SystemExit('Укажи DISCORD_TOKEN в локальном файле .env. Инструкция: README.md')
    bot.run(token)
