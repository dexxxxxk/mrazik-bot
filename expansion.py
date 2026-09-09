"""Persistent inventory, maze navigation and public collectible drops."""
import asyncio
import random
import discord
from discord import app_commands
from game import GameError
from content import OUTFITS


class NavButton(discord.ui.DynamicItem[discord.ui.Button],template=r'mrazik:nav:(?P<page>home|bag|maze|games)'):
    def __init__(self,page='home'):
        self.page=page
        names={'home':'↩ В меню','bag':'🎒 Инвентарь','maze':'🧩 Лабиринт','games':'🎮 Игры'}
        super().__init__(discord.ui.Button(label=names[page],custom_id=f'mrazik:nav:{page}',row=4))
    @classmethod
    async def from_custom_id(cls,i,item,match):return cls(match['page'])
    async def callback(self,i):
        try:
            if not await i.client.features.allowed(i,True):return
            await i.client.expansion.navigate(i,self.page)
        except Exception as error:await i.client.features.respond_error(i,error)


class OpenChest(discord.ui.DynamicItem[discord.ui.Button],template=r'mrazik:open:(?P<cid>[0-9a-f]{32})'):
    def __init__(self,cid):
        self.cid=cid
        super().__init__(discord.ui.Button(label='Открыть следующий сундук',style=discord.ButtonStyle.success,custom_id='mrazik:open:'+cid))
    @classmethod
    async def from_custom_id(cls,i,item,match):return cls(match['cid'])
    async def callback(self,i):
        app=i.client.expansion
        try:
            await i.response.defer()
            result=app.game.open_loot(i.guild_id,i.user.id,self.cid)
            embed,view=app.bag(i.guild_id,i.user.id,result['text'])
            await i.edit_original_response(embed=embed,view=view,attachments=[])
        except Exception as error:await app.bot.features.respond_error(i,error)


class MazeMove(discord.ui.DynamicItem[discord.ui.Button],template=r'mrazik:maze:(?P<mid>[0-9a-f]{32}):(?P<direction>[nswe])'):
    def __init__(self,mid,direction,disabled=False):
        self.mid,self.direction=mid,direction
        super().__init__(discord.ui.Button(label={'n':'⬆','s':'⬇','w':'⬅','e':'➡'}[direction],
            custom_id=f'mrazik:maze:{mid}:{direction}',disabled=disabled))
    @classmethod
    async def from_custom_id(cls,i,item,match):return cls(match['mid'],match['direction'])
    async def callback(self,i):
        app=i.client.expansion
        try:
            await i.response.defer()
            lock=app.maze_locks.setdefault(self.mid,asyncio.Lock())
            async with lock:
                m,reward=app.game.move_maze(i.guild_id,i.user.id,i.channel_id,i.message.id,self.mid,self.direction)
                embed,view=app.maze_card(m,reward)
                await i.edit_original_response(embed=embed,view=view,attachments=[])
        except Exception as error:await app.bot.features.respond_error(i,error)


def public_channels(guild):
    if not guild.me:return []
    result=[]
    for channel in guild.text_channels:
        if not channel.permissions_for(guild.default_role).view_channel:continue
        p=channel.permissions_for(guild.me)
        if p.view_channel and p.send_messages and p.embed_links and p.attach_files and p.read_message_history:result.append(channel)
    return result


class GameSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder='Начать личную игру',options=[discord.SelectOption(label=n,value=v) for n,v in
            [('Викторина','quiz'),('Три сундука','chests'),('Угадай число','guess'),('Рыбалка','fish')]])
    async def callback(self,i):
        service=i.client.features
        try:
            if await service.allowed(i):await service.personal(i,self.values[0])
        except Exception as error:await service.respond_error(i,error)


class Expansion:
    def __init__(self,bot,game,home_factory):
        self.bot,self.game,self.home_factory=bot,game,home_factory
        self.maze_locks={}

    def bag(self,g,u,result=''):
        items=self.game.inventory(g,u)
        embed=discord.Embed(title='🎒 Твой инвентарь',color=0x9BA95B,
            description=(result+'\n\n' if result else '')+f'Неоткрытых сундуков: **{len(items)}**.\n'
            'В каждом: 20% — один из 10 редких образов; 80% — 50–120 монет и 15–35 XP. '
            'Повтор редкого образа заменяется на 150 монет и 40 XP. Все награды сундука — без дневного лимита.\n'
            'Редкая одежда остаётся в /гардероб. Посмотреть её можно в /альбом.')
        view=discord.ui.View(timeout=None)
        if items:view.add_item(OpenChest(items[0]['id']))
        view.add_item(NavButton())
        return embed,view

    def maze_card(self,m,reward=None):
        grid=m['grid']
        picture='\n'.join(''.join('👺' if (x,y)==(m['x'],m['y']) else '🏁' if (x,y)==(7,7) else '⬛' if grid[y][x] else '⬜' for x in range(9)) for y in range(9))
        text=f"{picture}\nХодов: {m['moves']}. Дойди до 🏁. Время: <t:{int(m['expires'])}:R>."
        if reward is not None:text+=f'\nВыход найден! +{reward[0]} монет и +{reward[1]} XP.'+self.bot.features.limit_notice(m['guild'],m['uid'],reward)
        elif m['done']:text+='\nПоход завершён, награда уже начислена.'
        else:text+='\nНаграда: 40 монет и 25 XP без дневного лимита.'
        embed=discord.Embed(title='🧩 Лабиринт Мразика',description=text,color=0x9BA95B)
        view=discord.ui.View(timeout=None)
        for d in ['n','w','s','e']:view.add_item(MazeMove(m['id'],d,bool(m['done'])))
        view.add_item(NavButton())
        return embed,view

    async def navigate(self,i,page):
        # Public buttons open a private panel; private panels are edited in place.
        if page=='maze' and not await self.bot.features.allowed(i):return
        private=i.message is not None and i.message.flags.ephemeral
        if private:await i.response.defer()
        else:await i.response.defer(ephemeral=True,thinking=True)
        maze=None
        if page=='bag':embed,view=self.bag(i.guild_id,i.user.id)
        elif page=='maze':
            maze=self.game.start_maze(i.guild_id,i.user.id,i.channel_id)
            embed,view=self.maze_card(maze)
        elif page=='games':
            embed=self.bot.features.help_embed('games');view=discord.ui.View(timeout=600)
            view.add_item(GameSelect());view.add_item(NavButton('maze'));view.add_item(NavButton('bag'));view.add_item(NavButton())
        else:
            embed=discord.Embed(title='Мразик • личное меню',description='Твой питомец, игры и коллекция. Выбирай действие ниже.',color=0x9BA95B)
            view=self.home_factory()
        if private:message=await i.edit_original_response(embed=embed,view=view,attachments=[],content=None)
        else:message=await i.followup.send(embed=embed,view=view,ephemeral=True,wait=True)
        if maze:self.game.db.execute('UPDATE maze_runs SET message=?,channel=? WHERE id=?',(message.id,i.channel_id,maze['id']))

    def install(self):
        tree=self.bot.tree
        @tree.command(name='инвентарь',description='Собранные сундуки: открыть и получить редкий образ, опыт или золото')
        @app_commands.guild_only()
        async def bag(i:discord.Interaction):await self.navigate(i,'bag')
        @tree.command(name='лабиринт',description='Личный случайный лабиринт на кнопках: 10 минут, 40 монет и 25 XP за выход')
        @app_commands.guild_only()
        async def maze(i:discord.Interaction):await self.navigate(i,'maze')
        @tree.command(name='сундук_события',description='Совместимость: включить случайные события; подробные настройки — /автопосты')
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        async def setting(i:discord.Interaction,включить:bool):
            if not self.game.settings(i.guild_id):raise GameError('Сначала /настройка — выбери основной игровой канал.')
            cfg=self.bot.roaming.state(i.guild_id)
            self.bot.roaming.configure(i.guild_id,включить,cfg['min_minutes'],cfg['max_minutes'])
            await i.response.send_message('Случайные события включены. Первая попытка через минуту. Проверка: /автопост_статус.' if включить else 'Новые случайные сундуки выключены. Уже полученные остаются в инвентаре.',ephemeral=True)
