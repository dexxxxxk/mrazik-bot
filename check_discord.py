"""Offline Discord adapter smoke check; requires pip install -r requirements.txt.
Never logs in, syncs commands or sends messages.
"""
import asyncio
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import discord


async def check():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ['DATABASE_PATH']=str(Path(tmp)/'check.sqlite3')
        import bot
        response=SimpleNamespace(send_message=AsyncMock())
        interaction=SimpleNamespace(guild_id=1,channel_id=100,response=response)
        assert not await bot.allowed(interaction)
        bot.game.configure(1,200,12,False)
        assert not await bot.allowed(interaction)
        assert all(call.kwargs.get('ephemeral') is True for call in response.send_message.await_args_list)
        interaction.channel_id=200
        assert await bot.allowed(interaction)
        assert await bot.allowed(SimpleNamespace(guild_id=1,channel_id=999,response=response),True)
        for owner in [10,20]:
            obj=bot.pet_card(1,owner)
            assert 'личный питомец' in obj['embed'].title
            obj['file'].close()
        channel=Mock(spec=discord.TextChannel)
        channel.id=333;channel.guild=SimpleNamespace(id=1);channel.mention='<#333>'
        channel.permissions_for.return_value=discord.Permissions(view_channel=True,send_messages=True,embed_links=True,attach_files=True,read_message_history=True)
        client=SimpleNamespace(get_channel=Mock(return_value=None),fetch_channel=AsyncMock(return_value=channel))
        setup_i=SimpleNamespace(guild_id=1,guild=SimpleNamespace(me=object()),client=client,
            response=SimpleNamespace(defer=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))
        await bot.setup_command.callback(setup_i,SimpleNamespace(id=333),15,True)
        assert bot.game.settings(1)['channel']==333
        assert bot.game.settings(1)['automatic']==1
        setup_i.response.defer.assert_awaited_once_with(ephemeral=True)
        channel.permissions_for.return_value=discord.Permissions.none()
        try:
            await bot.setup_command.callback(setup_i,SimpleNamespace(id=333),16,False)
            raise AssertionError('Missing channel permissions must reject configuration')
        except bot.GameError: pass
        assert bot.game.settings(1)['hour']==15
        option=next(o for o in bot.setup_command.to_dict(bot.bot.tree)['options'] if o['name']=='канал')
        assert option['type']==7 and set(option['channel_types'])=={0,5}
        assert bot.bot.intents.guilds
        assert not bot.bot.intents.message_content
        assert bot.HomeView().is_persistent()
        assert bot.PetView().is_persistent()
        cmds=bot.bot.tree.get_commands()
        assert len(cmds)==31, len(cmds)
        assert {'мразик','сундуки','угадай','рыбалка','роли_настроить','роль','события','событие'} <= {c.name for c in cmds}
        assert 'гнидь' not in {c.name for c in cmds}
        for cmd in cmds:
            payload=cmd.to_dict(bot.bot.tree)
            assert 1<=len(payload['name'])<=32
            assert 1<=len(payload['description'])<=100
        for key in [*bot.OUTFITS,'hungry','sleep','laugh','victory']:
            obj=bot.card('Проверка','Проверка',key)
            assert 'file' in obj, key
            obj['file'].close()
        e=bot.game.start_activity(1,2,3,'chests')
        assert bot.bot.features.event_view(e).is_persistent()
        catalogue=[]
        for category in ['start','games','collection','social','admin']:
            catalogue.extend(f.name.split()[0][1:] for f in bot.bot.features.help_embed(category).fields)
        assert set(catalogue)=={c.name for c in cmds}
        assert len(catalogue)==len(cmds)
        view=bot.DuelView(1,2)
        assert len(view.children)==5
        view.stop()
        print(f'OK: {len(cmds)} slash commands, persistent views, channel isolation, personal pet cards, 16 PNG assets; no network connection.')
        bot.game.db.close()


if __name__=='__main__': asyncio.run(check())
