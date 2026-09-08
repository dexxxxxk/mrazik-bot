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
        print(f'OK: {len(cmds)} slash commands, persistent views, 16 PNG assets; no network connection.')
        bot.game.db.close()


if __name__=='__main__': asyncio.run(check())
