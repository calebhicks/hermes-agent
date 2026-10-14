"""Native startup and first-use credential behavior; synthetic provider only."""
from pathlib import Path
import json
import subprocess

import pytest

from agent.secret_scope import get_secret, set_secret_scope, reset_secret_scope, set_multiplex_active
from agent.secret_sources import onepassword
from hermes_cli import env_loader


@pytest.mark.parametrize('source,aliases,multiplex,eager', [
    ('conductor',False,False,False), ('cli',False,False,True),
    ('conductor',True,False,True), ('conductor',False,True,True),
])
def test_native_startup_defers_only_opted_in_scope(tmp_path, monkeypatch, source, aliases, multiplex, eager):
    home=tmp_path/'profile';home.mkdir()
    cfg={'secrets':{'profile_alias':aliases,'onepassword':{
        'enabled':True,'lazy_sources':['conductor'],'cache_ttl_seconds':0,
        'env':{'TEST_MODEL_API_KEY':'op://test/model/key','TEST_UNUSED_API_KEY':'op://test/unused/key'}}}}
    (home/'config.yaml').write_text(json.dumps(cfg))
    (home/'.env').write_text('OP_SERVICE_ACCOUNT_TOKEN=synthetic-bootstrap\nTEST_MODEL_API_KEY=old-value\n')
    monkeypatch.setenv('HERMES_HOME',str(home));monkeypatch.setenv('HERMES_SESSION_SOURCE',source)
    monkeypatch.delenv('TEST_UNUSED_API_KEY',raising=False)
    monkeypatch.setattr(onepassword,'find_op',lambda *_:Path('/synthetic/op'))
    calls=[]
    def read(_binary,reference,**kwargs):
        calls.append(reference)
        assert kwargs['token_value']=='synthetic-bootstrap'
        return 'synthetic-'+reference.split('/')[-2]
    monkeypatch.setattr(onepassword,'_run_op_read',read)
    env_loader.reset_secret_source_cache();set_multiplex_active(multiplex)
    try:
        env_loader.load_hermes_dotenv(hermes_home=home)
        assert len(calls)==(2 if eager else 0)
        if not eager:
            token=set_secret_scope({'TEST_MODEL_API_KEY':'old-value'})
            try:
                assert get_secret('TEST_MODEL_API_KEY')=='old-value'
                assert calls==[]
                assert get_secret('UNCONFIGURED_API_KEY') is None
            finally:reset_secret_scope(token)
            assert get_secret('TEST_MODEL_API_KEY')=='synthetic-model'
            assert get_secret('TEST_MODEL_API_KEY')=='synthetic-model'
            assert calls==['op://test/model/key']
            assert 'TEST_UNUSED_API_KEY' not in env_loader.get_secret_source_values(home)
        assert not (home/'cache/op_cache.json').exists()
    finally:
        set_multiplex_active(False);env_loader.reset_secret_source_cache()


def test_lazy_mcp_does_not_resolve_unselected_or_unused_credentials(tmp_path, monkeypatch):
    from tools import mcp_tool_config, mcp_tool_discovery, mcp_tool
    from hermes_cli.mcp_startup import set_mcp_server_filter
    from hermes_cli import config
    import asyncio
    home=tmp_path/'profile';home.mkdir()
    settings={'secrets':{'profile_alias':False,'onepassword':{'enabled':True,
        'lazy_sources':['conductor'],'cache_ttl_seconds':0,
        'env':{'TEST_MCP_TOKEN':'op://test/mcp/key','TEST_DENIED_TOKEN':'op://test/denied/key'}}},
        'mcp_servers':{'allowed':{'url':'https://example.invalid/mcp','lazy':True,
            'headers':{'Authorization':'Bearer ${TEST_MCP_TOKEN}'}},
            'excluded':{'url':'https://example.invalid/unused','lazy':True,'headers':{'Authorization':'${TEST_DENIED_TOKEN}'}}}}
    (home/'config.yaml').write_text(json.dumps(settings))
    (home/'.env').write_text('OP_SERVICE_ACCOUNT_TOKEN=synthetic-bootstrap\n')
    monkeypatch.setenv('HERMES_HOME',str(home));monkeypatch.setenv('HERMES_SESSION_SOURCE','conductor')
    monkeypatch.setattr(onepassword,'find_op',lambda *_:Path('/synthetic/op'))
    calls=[]
    def read(_binary,reference,**kwargs):calls.append(reference);return 'synthetic-token'
    monkeypatch.setattr(onepassword,'_run_op_read',read)
    env_loader.reset_secret_source_cache();set_mcp_server_filter(['allowed'])
    starts=[]
    async def fake_run(self,cfg):
        starts.append(cfg)
        self._ready.set()
    monkeypatch.setattr(mcp_tool.MCPServerTask,'run',fake_run)
    try:
        env_loader.load_hermes_dotenv(hermes_home=home)
        servers=mcp_tool_config._load_mcp_config()
        assert set(servers)=={'allowed','excluded'} and calls==[]
        assert servers['allowed']['headers']['Authorization']=='Bearer ${TEST_MCP_TOKEN}'
        asyncio.run(mcp_tool_discovery._connect_server('allowed',servers['allowed'].copy()))
        assert starts[0]['headers']['Authorization']=='Bearer synthetic-token'
        assert calls==['op://test/mcp/key']
        assert servers['allowed']['headers']['Authorization']=='Bearer ${TEST_MCP_TOKEN}'
        assert not (home/'cache/op_cache.json').exists()
    finally:
        set_mcp_server_filter(None);env_loader.reset_secret_source_cache()


@pytest.fixture
def lazy_profile(tmp_path, monkeypatch):
    home = tmp_path / 'lazy'
    home.mkdir()
    settings = {'profile_alias': False, 'onepassword': {
        'enabled': True, 'lazy_sources': ['conductor'], 'cache_ttl_seconds': 0,
        'env': {'TEST_LAZY_KEY': 'op://test/lazy/key'}}}
    (home / 'config.yaml').write_text(json.dumps({'secrets': settings}))
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_SESSION_SOURCE', 'conductor')
    monkeypatch.setenv('OP_SERVICE_ACCOUNT_TOKEN', 'synthetic-bootstrap')
    monkeypatch.delenv('TEST_LAZY_KEY', raising=False)
    monkeypatch.setattr(onepassword, 'find_op', lambda *_: Path('/synthetic/op'))
    calls = []
    def read(*args, **kwargs):
        calls.append(args[1])
        return 'value-${MISSING_LITERAL}'
    monkeypatch.setattr(onepassword, '_run_op_read', read)
    env_loader.reset_secret_source_cache()
    env_loader.load_hermes_dotenv(hermes_home=home)
    yield home, settings, calls
    env_loader.reset_secret_source_cache()
    set_multiplex_active(False)


def test_failure_once_and_reset_retries(lazy_profile, monkeypatch):
    home, _, calls = lazy_profile
    def failing(*args, **kwargs):
        calls.append(args[1])
        raise RuntimeError('synthetic failure')
    monkeypatch.setattr(onepassword, '_run_op_read', failing)
    assert get_secret('TEST_LAZY_KEY') is None
    assert get_secret('TEST_LAZY_KEY') is None
    assert len(calls) == 1
    env_loader.reset_secret_source_cache(home)
    assert get_secret('TEST_LAZY_KEY') is None
    assert len(calls) == 2


def test_reset_reloads_changed_policy(lazy_profile):
    home, settings, calls = lazy_profile
    assert get_secret('TEST_LAZY_KEY') == 'value-${MISSING_LITERAL}'
    settings['onepassword']['env'] = {}
    (home / 'config.yaml').write_text(json.dumps({'secrets': settings}))
    env_loader.reset_secret_source_cache(home)
    # Existing process env has native lifetime; removed mappings never fetch again.
    get_secret('TEST_LAZY_KEY')
    assert len(calls) == 1


def test_multiplex_transition_hydrates_instead_of_using_deferred_marker(lazy_profile):
    home, _, calls = lazy_profile
    set_multiplex_active(True)
    values = env_loader.hydrate_profile_secret_sources(home)
    assert values['TEST_LAZY_KEY'] == 'value-${MISSING_LITERAL}'
    assert len(calls) == 1


def test_second_source_and_aliases_decline_deferral(lazy_profile):
    _, settings, _ = lazy_profile
    assert env_loader._can_defer_onepassword(settings)
    settings['another_source'] = {'enabled': True}
    assert not env_loader._can_defer_onepassword(settings)


def test_override_false_keeps_existing_value(lazy_profile, monkeypatch):
    home, settings, calls = lazy_profile
    settings['onepassword']['override_existing'] = False
    (home / 'config.yaml').write_text(json.dumps({'secrets': settings}))
    monkeypatch.setenv('TEST_LAZY_KEY', 'inherited')
    env_loader.reset_secret_source_cache(home)
    assert get_secret('TEST_LAZY_KEY') == 'inherited'
    assert env_loader.get_secret_source_values(home)['TEST_LAZY_KEY'] == 'inherited'


@pytest.mark.parametrize('lazy', [False, True])
def test_mcp_resolves_exactly_once_at_eager_or_lazy_boundary(lazy_profile, monkeypatch, lazy):
    import asyncio
    from tools import mcp_tool_config, mcp_tool
    from hermes_cli import config
    _, _, calls = lazy_profile
    cfg = {'url': 'https://example.invalid', 'lazy': lazy,
           'headers': {'Authorization': '${TEST_LAZY_KEY}'}}
    monkeypatch.setattr(config, 'load_config', lambda: {'mcp_servers': {'test': cfg}})
    server_cfg = mcp_tool_config._load_mcp_config()['test']
    assert len(calls) == (0 if lazy else 1)
    received = []
    async def run(self, config):
        received.append(config)
        self._ready.set()
    monkeypatch.setattr(mcp_tool.MCPServerTask, 'run', run)
    asyncio.run(mcp_tool.MCPServerTask('test').start(server_cfg.copy()))
    assert received[0]['headers']['Authorization'] == 'value-${MISSING_LITERAL}'
    assert len(calls) == 1


def test_cancel_first_use_does_not_spawn_transport_or_block_loop(monkeypatch):
    import asyncio
    import threading
    from tools import mcp_tool_config, mcp_tool
    entered, release = threading.Event(), threading.Event()
    def resolve(config):
        entered.set()
        assert release.wait(5)
        return dict(config)
    monkeypatch.setattr(mcp_tool_config, '_interpolate_env_vars', resolve)
    async def drive():
        server = mcp_tool.MCPServerTask('cancel')
        task = asyncio.create_task(server.start(mcp_tool_config.DeferredMCPConfig({'command': 'fake'})))
        try:
            while not entered.is_set():
                await asyncio.sleep(.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert server._task is None
        finally:
            release.set()
    asyncio.run(drive())


def test_unmapped_reads_do_not_reparse_policy(lazy_profile, monkeypatch):
    home, _, calls = lazy_profile
    loads = []
    original = env_loader._load_secrets_config
    def load(home):
        loads.append(home)
        return original(home)
    monkeypatch.setattr(env_loader, '_load_secrets_config', load)
    for _ in range(3):
        assert get_secret('UNMAPPED') is None
    assert loads == []
    env_loader.reset_secret_source_cache()
    for _ in range(3):
        assert get_secret('UNMAPPED') is None
    assert loads == [home]
    assert get_secret('TEST_LAZY_KEY') == 'value-${MISSING_LITERAL}'
    assert len(calls) == 1
    assert loads == [home]


def test_eager_hydration_discards_pending_registration(lazy_profile):
    home, _, calls = lazy_profile
    env_loader.hydrate_profile_secret_sources(home)
    assert len(calls) == 1
    get_secret('TEST_LAZY_KEY')
    assert len(calls) == 1


def test_cache_enabled_policy_stays_eager(lazy_profile):
    _, settings, _ = lazy_profile
    settings['onepassword'].pop('cache_ttl_seconds')
    assert not env_loader._can_defer_onepassword(settings)
    settings['onepassword']['cache_ttl_seconds'] = 300
    assert not env_loader._can_defer_onepassword(settings)


def test_process_env_changes_remain_visible_after_first_use(lazy_profile, monkeypatch):
    _, _, calls = lazy_profile
    assert get_secret('TEST_LAZY_KEY') == 'value-${MISSING_LITERAL}'
    monkeypatch.setenv('TEST_LAZY_KEY', 'rotated-in-process')
    assert get_secret('TEST_LAZY_KEY') == 'rotated-in-process'
    assert len(calls) == 1


def test_lazy_connection_reuse_compares_resolved_profile_credentials():
    from types import SimpleNamespace
    from tools.mcp_tool_config import DeferredMCPConfig
    from tools.mcp_tool_registration import _same_server_route
    cfg = DeferredMCPConfig({'url':'https://example.invalid', 'headers':{'Authorization':'${TEST_LAZY_KEY}'}})
    live = SimpleNamespace(_config={'url':'https://example.invalid','headers':{'Authorization':'first-profile'}})
    set_multiplex_active(True)
    token = set_secret_scope({'TEST_LAZY_KEY':'first-profile'})
    try:
        assert _same_server_route(live,cfg)
        second = set_secret_scope({'TEST_LAZY_KEY':'second-profile'})
        try: assert not _same_server_route(live,cfg)
        finally: reset_secret_scope(second)
    finally:
        reset_secret_scope(token)
        set_multiplex_active(False)


def test_real_schema_write_and_lazy_registration_preserve_unresolved_config(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tools import mcp_schema_cache as cache, mcp_tool_registration as registration, mcp_tool
    from tools.mcp_tool_config import DeferredMCPConfig
    monkeypatch.setattr(cache, '_cache_path', lambda: tmp_path/'schema.json')
    monkeypatch.setattr(registration, '_select_utility_schemas', lambda *args: [])
    for name in ('_lazy_server_configs','_lazy_server_fingerprints','_lazy_server_tool_names'):
        monkeypatch.setattr(mcp_tool,name,{})
    cfg=DeferredMCPConfig({'command':'${TEST_COMMAND}', 'lazy':True,
                          'env':{'TOKEN':'${TEST_LAZY_KEY}'}})
    tool=SimpleNamespace(name='read', description='Synthetic read', inputSchema={'type':'object'},
                         annotations=SimpleNamespace(readOnlyHint=True))
    server=SimpleNamespace(_tools=[tool],_list_cache_meta={})
    registration._write_schema_cache('lazy_contract',server,cfg,lambda name:True)
    entry=cache.get_cached_entry('lazy_contract',cache.config_fingerprint(cfg.copy()))
    assert entry is not None
    try:
        names=registration._register_from_cache_sync('lazy_contract',cfg,entry)
        assert names
        pending=mcp_tool._lazy_server_configs['lazy_contract']
        assert isinstance(pending,DeferredMCPConfig)
        assert pending['env']['TOKEN']=='${TEST_LAZY_KEY}'
    finally:
        for name in locals().get('names',[]):
            registration._deregister_mcp_tool_all_scopes('lazy_contract',name)


def test_malformed_mapping_declines_lazy_policy(lazy_profile):
    _, settings, _ = lazy_profile
    settings['onepassword']['env'] = None
    assert not env_loader._can_defer_onepassword(settings)


def test_malformed_mcp_entry_does_not_hide_other_servers(monkeypatch):
    from hermes_cli import config
    from tools.mcp_tool_config import _load_mcp_config
    monkeypatch.setattr(config,'load_config',lambda:{'mcp_servers':{'blank':None,'valid':{'command':'fake','lazy':True}}})
    assert set(_load_mcp_config()) == {'valid'}


def test_reuse_resolution_failure_declines_only_that_connection(monkeypatch):
    from types import SimpleNamespace
    from tools import mcp_tool_config, mcp_tool_registration as registration
    cfg=mcp_tool_config.DeferredMCPConfig({'url':'https://example.invalid'})
    def fail(_):raise RuntimeError('synthetic')
    monkeypatch.setattr(mcp_tool_config,'_interpolate_env_vars',fail)
    assert not registration._same_server_route(SimpleNamespace(_config={}),cfg)
    assert registration._same_server_route(SimpleNamespace(_config={'url':'https://other.invalid'}),{'url':'https://other.invalid'})


def test_stale_scope_route_check_runs_outside_core_lock(monkeypatch):
    from types import SimpleNamespace
    from tools import mcp_tool, mcp_tool_registration as registration
    import threading
    lock=threading.Lock()
    monkeypatch.setattr(mcp_tool,'_lock',lock)
    monkeypatch.setattr(mcp_tool,'_mcp_registry_scope',lambda:'test-profile')
    monkeypatch.setattr(mcp_tool,'_server_tool_scopes',{'test':{'test-profile'}})
    monkeypatch.setattr(mcp_tool,'_servers',{'test':SimpleNamespace(session=object())})
    monkeypatch.setattr(registration,'_remove_server_scope',lambda *args:None)
    seen=[]
    def check(*args, **kwargs):
        assert lock.acquire(blocking=False),'credential resolution held global MCP lock'
        lock.release();seen.append(True);return False
    monkeypatch.setattr(registration,'_same_server_route',check)
    registration._register_connected_into_current_scope({'test':{'url':'https://example.invalid'}})
    assert len(seen)==2
