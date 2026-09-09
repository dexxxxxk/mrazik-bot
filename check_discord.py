"""Offline Discord adapter smoke check; requires pip install -r requirements.txt.
Never logs in, syncs commands or sends messages.
"""
import asyncio
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
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
        setup_i=SimpleNamespace(created_at=discord.utils.utcnow(),guild_id=1,guild=SimpleNamespace(me=object()),client=client,
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
        with patch.object(bot.game,'settings',side_effect=AssertionError('No DB before admin ACK')):
            assert await bot.allowed(SimpleNamespace(guild_id=1),True)
        expired=discord.NotFound(SimpleNamespace(status=404,reason='Not Found'),{'code':10062,'message':'Unknown interaction'})
        setup_i.command=SimpleNamespace(name='настройка');client.latency=0.1
        setup_i.response.defer=AsyncMock(side_effect=expired)
        state_before=bot.game.settings(1)
        try:
            await bot.setup_command.callback(setup_i,SimpleNamespace(id=333),17,False)
            raise AssertionError('Expired ACK must stop setup')
        except discord.NotFound:pass
        assert bot.game.settings(1)==state_before
        setup_i.response.send_message=AsyncMock()
        sent_before=setup_i.followup.send.await_count
        await bot.respond_error(setup_i,expired)
        setup_i.response.send_message.assert_not_awaited()
        assert setup_i.followup.send.await_count==sent_before
        setup_i.response.is_done=Mock(return_value=False)
        setup_i.response.send_message.side_effect=expired
        await bot.respond_error(setup_i,bot.GameError('Test expired error notification'))
        option=next(o for o in bot.setup_command.to_dict(bot.bot.tree)['options'] if o['name']=='канал')
        assert option['type']==7 and set(option['channel_types'])=={0,5}
        assert bot.bot.intents.guilds
        assert not bot.bot.intents.message_content
        assert bot.HomeView().is_persistent()
        assert bot.PetView().is_persistent()
        cmds=bot.bot.tree.get_commands()
        assert len(cmds)==35, len(cmds)
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
        bot.game.user(1,777)
        for key in bot.OUTFITS:bot.game.db.execute('INSERT OR IGNORE INTO owned VALUES(?,?,?)',(1,777,key))
        menu_i=SimpleNamespace(guild_id=1,user=SimpleNamespace(id=777),message=None,
            response=SimpleNamespace(send_message=AsyncMock()),original_response=AsyncMock())
        for command in [bot.wardrobe_command,bot.dress_command]:
            await command.callback(menu_i)
            payload=menu_i.response.send_message.call_args.kwargs
            menus=[c for c in payload['view'].children if isinstance(c,discord.ui.Select)]
            assert len(menus)==2 and all(len(m.options)<=25 for m in menus)
            assert {o.value for m in menus for o in m.options}==set(bot.OUTFITS)
            payload['file'].close();payload['view'].stop()
        assert any(c.value=='storm' for c in await bot.album_autocomplete(menu_i,'ворчания'))
        view=bot.DuelView(1,2)
        assert len(view.children)==6
        view.stop()
        print(f'OK: {len(cmds)} slash commands, persistent views, channel isolation, personal pet cards, 36 PNG assets; no network connection.')
        bot.game.db.close()


if __name__=='__main__': asyncio.run(check())
