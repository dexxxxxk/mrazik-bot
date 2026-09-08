"""Offline Discord adapter smoke check; requires pip install -r requirements.txt.
Never logs in, syncs commands or sends messages.
"""
import asyncio
import os
import tempfile
from pathlib import Path


async def check():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ['DATABASE_PATH']=str(Path(tmp)/'check.sqlite3')
        import bot
        assert bot.bot.intents.guilds
        assert not bot.bot.intents.message_content
        assert bot.HomeView().is_persistent()
        assert bot.PetView().is_persistent()
        cmds=bot.bot.tree.get_commands()
        assert len(cmds)==22, len(cmds)
        for cmd in cmds:
            payload=cmd.to_dict(bot.bot.tree)
            assert 1<=len(payload['name'])<=32
            assert 1<=len(payload['description'])<=100
        for key in [*bot.OUTFITS,'hungry','sleep','laugh','victory']:
            obj=bot.card('Проверка','Проверка',key)
            assert 'file' in obj, key
            obj['file'].close()
        view=bot.DuelView(1,2)
        assert len(view.children)==5
        view.stop()
        print(f'OK: {len(cmds)} slash commands, persistent views, 11 PNG assets; no network connection.')
        bot.game.db.close()


if __name__=='__main__': asyncio.run(check())
