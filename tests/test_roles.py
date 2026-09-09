import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import discord
from content import RANKS
from game import Game, GameError
from features import FeatureService


class Role:
    def __init__(self,id,name='rank',position=1,permissions=0):
        self.id,self.name,self.position=id,name,position
        self.permissions=discord.Permissions(permissions)
        self.managed=False
    def is_default(self): return False
    def __ge__(self,other): return self.position>=other.position


class RoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.game=Game(str(Path(self.tmp.name)/'game.db'))
        self.roles={n+1:Role(n+1,name) for n,(_,name) in enumerate(RANKS)}
        for n,(xp,_) in enumerate(RANKS): self.game.remember_role(1,xp,n+1)
        self.unrelated=Role(99,'moderator',permissions=8)
        self.member=SimpleNamespace(roles=[self.roles[1],self.unrelated],add_roles=AsyncMock(),remove_roles=AsyncMock())
        self.guild=SimpleNamespace(id=1,get_role=self.roles.get,fetch_member=AsyncMock(return_value=self.member),
            me=SimpleNamespace(guild_permissions=discord.Permissions(manage_roles=True),top_role=Role(100,position=10)))
        self.service=FeatureService(None,self.game,None,None,None)
        with self.game.tx(): self.game._reward(1,10,0,500,False)
        self.game.enable_roles(1,True)

    async def asyncTearDown(self):
        self.game.db.close();self.tmp.cleanup()

    async def test_upgrade_removes_only_managed_rank(self):
        await self.service.sync_role(self.guild,10)
        self.member.add_roles.assert_awaited_once_with(self.roles[3],reason='Опыт личного питомца Мразика',atomic=True)
        self.member.remove_roles.assert_awaited_once_with(self.roles[1],reason='Обновление игрового ранга',atomic=True)
        self.assertEqual(self.game.db.execute('SELECT count(*) FROM role_queue').fetchone()[0],0)

    async def test_privileged_role_or_hierarchy_is_rejected_before_any_mutation(self):
        self.roles[3].permissions=discord.Permissions(administrator=True)
        with self.assertRaises(GameError): await self.service.sync_role(self.guild,10)
        self.member.add_roles.assert_not_awaited(); self.member.remove_roles.assert_not_awaited()
        self.roles[3].permissions=discord.Permissions.none(); self.roles[3].position=20
        with self.assertRaises(GameError): await self.service.sync_role(self.guild,10)
        self.member.add_roles.assert_not_awaited()

    async def test_failed_add_does_not_remove_current_role(self):
        self.member.add_roles.side_effect=RuntimeError('network interrupted')
        with self.assertRaises(RuntimeError): await self.service.sync_role(self.guild,10)
        self.member.remove_roles.assert_not_awaited()
        self.assertEqual(self.game.db.execute('SELECT count(*) FROM role_queue').fetchone()[0],1)

    async def test_xp_earned_during_http_remains_queued(self):
        async def reward(*args,**kwargs):
            with self.game.tx(): self.game._reward(1,10,0,600,False)
        self.member.add_roles.side_effect=reward
        await self.service.sync_role(self.guild,10)
        self.assertEqual(self.game.db.execute('SELECT xp FROM role_queue').fetchone()[0],1100)

    async def test_disabled_roles_do_not_call_discord(self):
        self.game.enable_roles(1,False)
        await self.service.sync_role(self.guild,10)
        self.guild.fetch_member.assert_not_awaited()


    async def test_add_four_roles_preserves_six_ids_and_is_repeatable(self):
        self.game.db.execute('DELETE FROM rank_roles WHERE guild=1 AND threshold>4000')
        for n in range(7,11):self.roles.pop(n)
        old=self.game.rank_role_map(1).copy()
        async def create(**kw):
            rid=max(self.roles)+1;role=Role(rid,kw['name']);self.roles[rid]=role;return role
        self.guild.create_role=AsyncMock(side_effect=create)
        await self.service.setup_roles(self.guild)
        self.assertEqual(self.guild.create_role.await_count,4)
        self.assertEqual({t:self.game.rank_role_map(1)[t] for t in old},old)
        self.assertEqual(len(self.game.rank_role_map(1)),10)
        await self.service.setup_roles(self.guild)
        self.assertEqual(self.guild.create_role.await_count,4)

    async def test_old_six_roles_keep_working_before_setup(self):
        self.game.db.execute('DELETE FROM rank_roles WHERE guild=1 AND threshold>4000')
        await self.service.sync_role(self.guild,10)
        self.member.add_roles.assert_awaited_once_with(self.roles[3],reason='Опыт личного питомца Мразика',atomic=True)

    async def test_existing_high_xp_gets_tenth_rank_without_losing_other_roles(self):
        self.game.db.execute('UPDATE users SET xp=26000 WHERE guild=1 AND uid=10')
        await self.service.sync_role(self.guild,10)
        self.member.add_roles.assert_awaited_once_with(self.roles[10],reason='Опыт личного питомца Мразика',atomic=True)
        self.member.remove_roles.assert_awaited_once_with(self.roles[1],reason='Обновление игрового ранга',atomic=True)
