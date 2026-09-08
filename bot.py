"""Гнидь: развлечения для Мразотного Логова. Python 3.11+."""
import asyncio
import logging
import os
import random
import uuid
from pathlib import Path
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from content import OUTFITS, RANKS, QUIZ, FORTUNES, badges, rank
from game import Game, GameError

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
DB_PATH = Path(os.getenv('DATABASE_PATH', 'data/gnid.sqlite3'))
game = Game(str(DB_PATH if DB_PATH.is_absolute() else ROOT / DB_PATH), os.getenv('TZ','Europe/Moscow'))
log = logging.getLogger('gnid')


def reward_text(reward):
    return f'+{reward[0]} монеток · +{reward[1]} опыта'


def card(title, description, art='base'):
    embed = discord.Embed(title=title, description=description, color=0x9BA95B)
    embed.set_footer(text='Мразотное Логово • Гнидь всё записывает')
    path = ROOT / 'assets' / f'{art}.png'
    kwargs = {'embed':embed}
    if path.is_file():
        embed.set_image(url=f'attachment://{art}.png')
        kwargs['file'] = discord.File(path, filename=f'{art}.png')
    return kwargs


async def respond_error(i, error):
    error = getattr(error, 'original', error)
    if isinstance(error, GameError):
        text = str(error)
    elif isinstance(error, app_commands.MissingPermissions):
        text = 'Это действие для участника с правом «Управлять сервером».'
    elif isinstance(error, app_commands.CommandOnCooldown):
        text = f'Не спеши. Повтори через {error.retry_after:.0f} сек.'
    else:
        log.error('Interaction failed', exc_info=(type(error),error,error.__traceback__))
        text = 'Гнидь споткнулся. Попробуй снова; если повторится — сообщи владельцу бота.'
    if i.response.is_done():
        await i.followup.send(text, ephemeral=True)
    else:
        await i.response.send_message(text, ephemeral=True)


async def allowed(i, bypass_channel=False):
    if not i.guild_id:
        await i.response.send_message('Гнидь живёт на сервере. В личке не играем.', ephemeral=True)
        return False
    config = game.settings(i.guild_id)
    if not bypass_channel and config and i.channel_id != config['channel']:
        await i.response.send_message(f"Играем в <#{config['channel']}>.",ephemeral=True)
        return False
    return True


class Tree(app_commands.CommandTree):
    async def interaction_check(self, i):
        bypass = i.command and i.command.name in {'настройка','помощь','проверить','заявки','панель'}
        return await allowed(i, bypass)

    async def on_error(self, i, error):
        await respond_error(i,error)


class SafeView(discord.ui.View):
    def __init__(self, *, timeout=120):
        super().__init__(timeout=timeout)
        self.message = None

    async def interaction_check(self, i):
        return await allowed(i)

    async def on_error(self, i, error, item):
        await respond_error(i,error)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def send_view(i, view, **kwargs):
    await i.response.send_message(view=view, **kwargs)
    view.message = await i.original_response()


def profile(g,u):
    p = game.user(g,u)
    own = game.owned(g,u)
    awards = badges(p,own)
    nxt = next((x for x in RANKS if x[0]>p['xp']),None)
    body = (f"**{rank(p['xp'])}**\n🪙 {p['coins']} монеток · ✨ {p['xp']} опыта\n"
            f"Серия визитов: {p['streak']} · Победы: {p['wins']} · Ответы: {p['correct']}\n"
            f"Костюм: {OUTFITS[p['outfit']]['name']}\n"
            + (f'Следующий ранг: {nxt[1]} — ещё {nxt[0]-p["xp"]} опыта.\n' if nxt else 'Высший ранг достигнут.\n')
            + ('\n'+' · '.join(awards) if awards else '\nДостижения ещё впереди.'))
    return card('Паспорт обитателя',body,p['outfit'])


def pet_card(g):
    p=game.pet(g)
    art = 'hungry' if p['food']<20 else ('sleep' if p['energy']<20 else p['outfit'])
    return card('Гнидь • общий питомец',
        f"**{rank(p['xp'])}** · {p['xp']} общего опыта\n"
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
        await i.response.send_message(**card(phrases[action],reward_text(r),art),ephemeral=True)

    @discord.ui.button(label='Покормить',emoji='🥣',custom_id='gnid:feed:v1',style=discord.ButtonStyle.success)
    async def feed(self,i,button): await self.care(i,'feed')

    @discord.ui.button(label='Поиграть',emoji='🎲',custom_id='gnid:play:v1')
    async def play(self,i,button): await self.care(i,'play')

    @discord.ui.button(label='Уложить',emoji='💤',custom_id='gnid:sleep:v1')
    async def sleep(self,i,button): await self.care(i,'sleep')

    @discord.ui.button(label='Обновить состояние',custom_id='gnid:refresh:v1',row=1)
    async def refresh(self,i,button):
        await i.response.send_message(**pet_card(i.guild_id),ephemeral=True)


class HomeView(SafeView):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label='Мой профиль',emoji='🪪',custom_id='gnid:profile:v1')
    async def me(self,i,button):
        await i.response.send_message(**profile(i.guild_id,i.user.id),ephemeral=True)

    @discord.ui.button(label='Подачка дня',emoji='🪙',custom_id='gnid:daily:v1',style=discord.ButtonStyle.success)
    async def daily(self,i,button): await daily_response(i)

    @discord.ui.button(label='Наш Гнидь',emoji='🐾',custom_id='gnid:pet:v1')
    async def pet(self,i,button):
        await i.response.send_message(**pet_card(i.guild_id),view=PetView(),ephemeral=True)

    @discord.ui.button(label='Магазин',emoji='👕',custom_id='gnid:shop:v1',row=1)
    async def shop(self,i,button): await shop_response(i)

    @discord.ui.button(label='Викторина',emoji='🧠',custom_id='gnid:quiz:v1',row=1)
    async def quiz(self,i,button): await quiz_response(i)

    @discord.ui.button(label='Задание дня',emoji='🎯',custom_id='gnid:quest:v1',row=1)
    async def quest(self,i,button): await quest_response(i)


class OutfitSelect(discord.ui.Select):
    def __init__(self, owner, mode, items):
        self.owner,self.mode=owner,mode
        options=[discord.SelectOption(label=OUTFITS[k]['name'],value=k,
                   description=f"{OUTFITS[k]['price']} монеток • от {OUTFITS[k]['xp']} опыта") for k in items]
        super().__init__(placeholder='Выбери тряпьё',options=options)

    async def callback(self,i):
        if i.user.id!=self.owner:
            return await i.response.send_message('Открой свой магазин или гардероб.',ephemeral=True)
        key=self.values[0]
        if self.mode=='buy':
            game.purchase(i.guild_id,i.user.id,key)
            text='Куплено! Надеть на свой профиль: /гардероб. На общего Гнидя: /одеть.'
        else:
            game.equip(i.guild_id,i.user.id,key,self.mode=='shared')
            text='Гнидь нарядился. И немедленно заважничал.'
        await i.response.send_message(**card(OUTFITS[key]['name'],text,key),ephemeral=True)


async def shop_response(i):
    body='\n'.join(f"**{v['name']}** — {v['price']} 🪙 · от {v['xp']} XP" for k,v in OUTFITS.items() if k!='base')
    body+='\n\nВыбор в меню сразу покупает костюм за игровые монетки. Вещи навсегда; бонусов к победе нет.'
    view=SafeView()
    view.add_item(OutfitSelect(i.user.id,'buy',[k for k in OUTFITS if k!='base']))
    await send_view(i,view,**card('Лавка подозрительного тряпья',body,'gopnik'),ephemeral=True)


class QuizView(SafeView):
    def __init__(self,owner,options,answer):
        super().__init__(timeout=45)
        self.owner,self.answer,self.done=owner,answer,False
        for label in options:
            b=discord.ui.Button(label=label)
            async def callback(i, choice=label):
                if i.user.id!=self.owner:
                    return await i.response.send_message('Это чужой вопрос. Открой /викторина.',ephemeral=True)
                if self.done: raise GameError('Ответ уже принят.')
                correct=choice==self.answer
                r=game.quiz_reward(i.guild_id,i.user.id,correct)
                self.done=True
                for child in self.children: child.disabled=True
                await i.response.edit_message(view=self)
                await i.followup.send(**card('Угадал. Подозрительно.' if correct else 'Мимо. Гнидь доволен.',
                    f'Верный ответ: **{self.answer}**\n{reward_text(r)}','victory' if correct else 'laugh'),ephemeral=True)
                self.stop()
            b.callback=callback
            self.add_item(b)


async def quiz_response(i):
    game.cooldown(i.guild_id,i.user.id,'quiz',60)
    question,options,answer=random.choice(QUIZ)
    options=random.sample(options,len(options))
    await send_view(i,QuizView(i.user.id,options,answer),
                    **card('Викторина Гнидя',question+'\n\n45 секунд. Один ответ. За верный: 25 монеток и 15 опыта.','mage'),ephemeral=True)


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
                    result+='Ничья. Гнидь присудил победу себе. Наград нет.'
                else:
                    winner,loser=(self.a,self.b) if (x-y)%3==2 else (self.b,self.a)
                    win_reward,lose_reward=game.duel_reward(i.guild_id,winner,loser)
                    result+=f'Победил <@{winner}>: {reward_text(win_reward)}\nСопернику: {reward_text(lose_reward)}'
                for child in self.children: child.disabled=True
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
        for child in self.children: child.disabled=True
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
        self.add_view(HomeView())
        self.add_view(PetView())
        gid=os.getenv('GUILD_ID','').strip()
        if gid:
            guild=discord.Object(id=int(gid))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.daily_posts.start()

    async def close(self):
        self.daily_posts.cancel()
        await super().close()
        game.db.close()

    @tasks.loop(minutes=1)
    async def daily_posts(self):
        for cfg in game.db.execute('SELECT * FROM settings WHERE automatic=1').fetchall():
            if game.now().hour<cfg['hour']: continue
            channel=self.get_channel(cfg['channel'])
            if not channel: continue
            if not game.claim_post(cfg['guild']): continue
            try:
                await channel.send(**card('Гнидь принёс задание дня',
                    game.quest()+'\n\nСдать: /сдать. После одобрения модератора: 80 монеток и 40 опыта.','chef'))
            except discord.HTTPException:
                # Keep the claim: an uncertain network delivery must not duplicate a post.
                log.warning('Daily post failed for guild %s; manual /задание remains available',cfg['guild'])

    @daily_posts.before_loop
    async def before_daily(self): await self.wait_until_ready()


bot=Gnid()


async def daily_response(i):
    r,streak=game.daily(i.guild_id,i.user.id)
    await i.response.send_message(**card('Держи. И не привыкай.',
        f'{reward_text(r)}\nСерия визитов: {streak} дн.','victory'),ephemeral=True)


async def quest_response(i):
    await i.response.send_message(**card('Задание дня',
        game.quest()+'\n\nСдай через /сдать с текстом или картинкой. Модератор проверит.\nНаграда: 80 монеток и 40 опыта.','chef'),ephemeral=True)


@bot.tree.command(name='помощь',description='Что умеет эта мразота')
@app_commands.guild_only()
async def help_command(i:discord.Interaction):
    await i.response.send_message(**card('Добро пожаловать в Логово',
        '**Начать:** /профиль · /ежедневно · /гнидь\n'
        '**Игры:** /викторина · /дуэль · /экспедиция · /предсказание\n'
        '**Коллекция:** /магазин · /гардероб · /одеть · /ранги · /альбом\n'
        '**Компания:** /участие · /кто · /задание · /сдать · /топ\n'
        '**Для модераторов:** /настройка · /панель · /заявки · /проверить\n\n'
        'Все монетки игровые. За сообщения наград нет. Игровой лимит в день: 300 монеток и 200 XP; '
        'подачка дня и одобренное задание начисляются отдельно.'),ephemeral=True)


@bot.tree.command(name='профиль',description='Монетки, ранг, достижения и твой образ Гнидя')
@app_commands.guild_only()
async def profile_command(i:discord.Interaction):
    await i.response.send_message(**profile(i.guild_id,i.user.id),ephemeral=True)


@bot.tree.command(name='ежедневно',description='Забрать подачку дня и продолжить серию визитов')
@app_commands.guild_only()
async def daily_command(i:discord.Interaction): await daily_response(i)


@bot.tree.command(name='гнидь',description='Посмотреть на общего Гнидя, покормить и поиграть')
@app_commands.guild_only()
async def pet_command(i:discord.Interaction):
    await i.response.send_message(**pet_card(i.guild_id),view=PetView(),ephemeral=True)


@bot.tree.command(name='магазин',description='Купить костюм за игровые монетки')
@app_commands.guild_only()
async def shop_command(i:discord.Interaction): await shop_response(i)


@bot.tree.command(name='гардероб',description='Твоя коллекция: выбрать образ для профиля')
@app_commands.guild_only()
async def wardrobe_command(i:discord.Interaction):
    view=SafeView()
    owned=game.owned(i.guild_id,i.user.id)
    view.add_item(OutfitSelect(i.user.id,'personal',owned))
    await send_view(i,view,**card('Твои тряпки','\n'.join(OUTFITS[k]['name'] for k in owned)),ephemeral=True)


@bot.tree.command(name='одеть',description='Надеть свой костюм на общего Гнидя; смена раз в 30 минут')
@app_commands.guild_only()
async def dress_command(i:discord.Interaction):
    view=SafeView()
    view.add_item(OutfitSelect(i.user.id,'shared',game.owned(i.guild_id,i.user.id)))
    await send_view(i,view,**card('Наряди общую мразоту','Выбирай из своей коллекции. Образ общий для всего сервера.'),ephemeral=True)


@bot.tree.command(name='ранги',description='Ранги участников и пороги опыта')
@app_commands.guild_only()
async def ranks_command(i:discord.Interaction):
    await i.response.send_message(**card('Лестница сомнительного успеха',
        '\n'.join(f'**{name}** — {xp} XP' for xp,name in RANKS)+'\n\nРанг в игровом профиле. Костюмы покупаются отдельно.','king'),ephemeral=True)


ART_NAMES={**{k:v['name'] for k,v in OUTFITS.items()},'hungry':'Голодный Гнидь',
           'sleep':'Спящая мразота','laugh':'Злорадство','victory':'Нечестная победа'}


@bot.tree.command(name='альбом',description='Посмотреть все 11 картинок Гнидя, в том числе костюмы до покупки')
@app_commands.guild_only()
@app_commands.choices(образ=[app_commands.Choice(name=v,value=k) for k,v in ART_NAMES.items()])
async def album_command(i:discord.Interaction,образ:app_commands.Choice[str]):
    key=образ.value
    info=OUTFITS.get(key)
    description=(f"Цена: {info['price']} монеток · Нужно {info['xp']} опыта.\nКупить: /магазин." if info and key!='base'
                 else 'Обитатель Логова во всей своей сомнительной красе.')
    await i.response.send_message(**card(ART_NAMES[key],description,key),ephemeral=True)


@bot.tree.command(name='викторина',description='Один вопрос, четыре кнопки, 45 секунд')
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


@bot.tree.command(name='экспедиция',description='Отправиться с Гнидем за сомнительным добром раз в 4 часа')
@app_commands.guild_only()
async def expedition_command(i:discord.Interaction):
    event,r=game.expedition(i.guild_id,i.user.id)
    await i.response.send_message(**card('Вылазка на помойку',event+'\n'+reward_text(r),'hobo'),ephemeral=True)


@bot.tree.command(name='предсказание',description='Сомнительная мудрость Гнидя')
@app_commands.guild_only()
async def fortune_command(i:discord.Interaction):
    game.cooldown(i.guild_id,i.user.id,'fortune',60)
    await i.response.send_message(**card('Гнидь видит твоё будущее',random.choice(FORTUNES),'mage'),ephemeral=True)


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
    await i.response.send_message(**card(title,f'Сегодня это <@{u}>.\nУчастие добровольное: /участие.','laugh'))


@bot.tree.command(name='топ',description='Десятка участников по опыту за неделю или за всё время')
@app_commands.guild_only()
async def top_command(i:discord.Interaction,за_всё_время:bool=False):
    rows=game.top(i.guild_id,not за_всё_время)
    body='\n'.join(f'{n}. <@{r[0]}> — {r[1]} XP' for n,r in enumerate(rows,1)) or 'Пока пусто. Самое время стать первым.'
    await i.response.send_message(**card('Слава Логова • '+('всё время' if за_всё_время else 'эта неделя'),body,'king'),ephemeral=True)


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
async def setup_command(i:discord.Interaction,канал:discord.TextChannel,час:app_commands.Range[int,0,23]=12,автопост:bool=False):
    perms=канал.permissions_for(i.guild.me)
    if not all([perms.view_channel,perms.send_messages,perms.embed_links,perms.attach_files,perms.read_message_history]):
        raise GameError('В канале нужны права: видеть канал, отправлять сообщения, вставлять ссылки, прикреплять файлы и читать историю.')
    game.configure(i.guild_id,канал.id,час,автопост)
    await i.response.send_message(f'Игровой канал: {канал.mention}. Автопост: {автопост}, час {час}:00 ({game.tz}).\nТеперь отправь /панель в игровом канале.',ephemeral=True)


@bot.tree.command(name='панель',description='Опубликовать главную панель Гнидя в игровом канале')
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def panel_command(i:discord.Interaction):
    if not await allowed(i): return
    await i.response.send_message(**card('Мразотное Логово',
        'Я Гнидь. Живу тут, жру тут, осуждаю тоже тут.\n\n'
        'Копи монетки, собирай тряпки, вызывай друзей на дуэли. Кнопки ниже — твой вход в Логово.\n'
        'Все команды: /помощь.'),view=HomeView())


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    token=os.getenv('DISCORD_TOKEN','').strip()
    if not token: raise SystemExit('Укажи DISCORD_TOKEN в локальном файле .env. Инструкция: README.md')
    bot.run(token)
