import asyncio,os,tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch

async def check():
    with tempfile.TemporaryDirectory() as d:
        os.environ['DATABASE_PATH']=str(Path(d)/'db')
        import bot
        remote=[NS(name=c.name,id=n,default_member_permissions=None) for n,c in enumerate(bot.bot.tree.get_commands())]
        for gid in ['', '123456789']:
            with patch.dict(os.environ,{'GUILD_ID':gid}),patch.object(bot.bot.tree,'sync',new=AsyncMock(return_value=remote)) as sync,patch.object(bot.bot.daily_posts,'start'),patch.object(bot.bot.features.worker,'start'),patch.object(bot.bot.roaming.worker,'start'),patch.object(bot.bot.boss.worker,'start'):
                await bot.bot.setup_hook()
                assert sync.await_count==1
                if gid:assert sync.call_args.kwargs['guild'].id==int(gid)
                else:assert not sync.call_args.kwargs
        view=bot.HomeView();assert view.is_persistent()
        assert len([c for c in view.children if getattr(c,'custom_id','').startswith('mrazik:auto:')])==3
        i=NS(guild=NS(id=1),guild_id=1,user=NS(guild_permissions=NS(manage_guild=False)),response=NS(send_message=AsyncMock(),defer=AsyncMock()),followup=NS(send=AsyncMock()))
        with patch.object(bot.bot.roaming,'configure') as configure:
            await bot.autopost_control(i,'enable');configure.assert_not_called()
            i.user.guild_permissions.manage_guild=True
            bot.game.configure(1,100,12,False)
            await bot.autopost_control(i,'enable');configure.assert_called_once_with(1,True,60,120)
        with patch.object(bot.bot.roaming,'status',new=AsyncMock()) as status:
            await bot.autopost_control(i,'status');status.assert_awaited_once_with(i)
        with patch.object(bot.bot.roaming,'send',new=AsyncMock(return_value='sent')) as send:
            await bot.autopost_control(i,'now');send.assert_awaited_once_with(i.guild,force=True)
        bot.game.db.close()
        print('OK: sync in global/guild modes; three persistent panel buttons; admin permission enforced')
asyncio.run(check())
