import json
from unittest import mock

import pytest

from ceph.deployment.service_spec import PlacementSpec, ServiceSpec
from cephadm import CephadmOrchestrator
from cephadm.upgrade import CephadmUpgrade, UpgradeState, UPGRADE_IMAGE_MIRROR_METHOD_REGISTRY
from cephadm.ssh import HostConnectionError
from cephadm.utils import ContainerInspectInfo
from orchestrator import OrchestratorError, DaemonDescription
from .fixtures import _run_cephadm, wait, with_host, with_service, \
    receive_agent_metadata, async_side_effect

from typing import List, Tuple, Optional


def _upgrade_test_daemon(hostname: str = 'host1') -> DaemonDescription:
    return DaemonDescription(
        hostname=hostname,
        daemon_type='mgr',
        daemon_id='0',
    )


@mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm('{}'))
def test_upgrade_start(cephadm_module: CephadmOrchestrator):
    with with_host(cephadm_module, 'test'):
        with with_host(cephadm_module, 'test2'):
            with with_service(cephadm_module, ServiceSpec('mgr', placement=PlacementSpec(count=2)), status_running=True):
                assert wait(cephadm_module, cephadm_module.upgrade_start(
                    'image_id', None)) == 'Initiating upgrade to image_id'

                assert wait(cephadm_module, cephadm_module.upgrade_status()
                            ).target_image == 'image_id'

                assert wait(cephadm_module, cephadm_module.upgrade_pause()
                            ) == 'Paused upgrade to image_id'

                assert wait(cephadm_module, cephadm_module.upgrade_resume()
                            ) == 'Resumed upgrade to image_id'

                assert wait(cephadm_module, cephadm_module.upgrade_stop()
                            ) == 'Stopped upgrade to image_id'


@mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm('{}'))
def test_upgrade_start_offline_hosts(cephadm_module: CephadmOrchestrator):
    with with_host(cephadm_module, 'test'):
        with with_host(cephadm_module, 'test2'):
            cephadm_module.offline_hosts = set(['test2'])
            with pytest.raises(OrchestratorError, match=r"Upgrade aborted - Some host\(s\) are currently offline: {'test2'}"):
                cephadm_module.upgrade_start('image_id', None)
            cephadm_module.offline_hosts = set([])  # so remove_host doesn't fail when leaving the with_host block


@mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm('{}'))
def test_upgrade_daemons_offline_hosts(cephadm_module: CephadmOrchestrator):
    with with_host(cephadm_module, 'test'):
        with with_host(cephadm_module, 'test2'):
            cephadm_module.upgrade.upgrade_state = UpgradeState('target_image', 0)
            with mock.patch("cephadm.serve.CephadmServe._run_cephadm", side_effect=HostConnectionError('connection failure reason', 'test2', '192.168.122.1')):
                _to_upgrade = [(DaemonDescription(daemon_type='crash', daemon_id='test2', hostname='test2'), True)]
                with pytest.raises(HostConnectionError, match=r"connection failure reason"):
                    cephadm_module.upgrade._upgrade_daemons(_to_upgrade, 'target_image', ['digest1'])


@mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm('{}'))
def test_do_upgrade_offline_hosts(cephadm_module: CephadmOrchestrator):
    with with_host(cephadm_module, 'test'):
        with with_host(cephadm_module, 'test2'):
            cephadm_module.upgrade.upgrade_state = UpgradeState('target_image', 0)
            cephadm_module.offline_hosts = set(['test2'])
            with pytest.raises(HostConnectionError, match=r"Host\(s\) were marked offline: {'test2'}"):
                cephadm_module.upgrade._do_upgrade()
            cephadm_module.offline_hosts = set([])  # so remove_host doesn't fail when leaving the with_host block


@mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm('{}'))
@mock.patch("cephadm.module.CephadmOrchestrator.remove_health_warning")
def test_upgrade_resume_clear_health_warnings(_rm_health_warning, cephadm_module: CephadmOrchestrator):
    with with_host(cephadm_module, 'test'):
        with with_host(cephadm_module, 'test2'):
            cephadm_module.upgrade.upgrade_state = UpgradeState('target_image', 0, paused=True)
            _rm_health_warning.return_value = None
            assert wait(cephadm_module, cephadm_module.upgrade_resume()
                        ) == 'Resumed upgrade to target_image'
            calls_list = [mock.call(alert_id) for alert_id in cephadm_module.upgrade.UPGRADE_ERRORS]
            _rm_health_warning.assert_has_calls(calls_list, any_order=True)


@mock.patch('cephadm.upgrade.CephadmUpgrade._get_current_version', lambda _: (17, 2, 6))
@mock.patch("cephadm.serve.CephadmServe._get_container_image_info")
def test_upgrade_check_with_ceph_version(_get_img_info, cephadm_module: CephadmOrchestrator):
    # This test was added to avoid screwing up the image base so that
    # when the version was added to it it made an incorrect image
    # The issue caused the image to come out as
    # quay.io/ceph/ceph:v18:v18.2.0
    # see https://tracker.ceph.com/issues/63150
    _img = ''

    def _fake_get_img_info(img_name):
        nonlocal _img
        _img = img_name
        return ContainerInspectInfo(
            'image_id',
            '18.2.0',
            'digest'
        )

    _get_img_info.side_effect = _fake_get_img_info
    cephadm_module.upgrade_check('', '18.2.0')
    assert _img == 'quay.io/ceph/ceph:v18.2.0'


@mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm('{}'))
@pytest.mark.parametrize("use_repo_digest",
                         [
                             False,
                             True
                         ])
def test_upgrade_run(use_repo_digest, cephadm_module: CephadmOrchestrator):
    with with_host(cephadm_module, 'host1'):
        with with_host(cephadm_module, 'host2'):
            cephadm_module.set_container_image('global', 'from_image')
            cephadm_module.use_repo_digest = use_repo_digest
            with with_service(cephadm_module, ServiceSpec('mgr', placement=PlacementSpec(host_pattern='*', count=2)),
                              CephadmOrchestrator.apply_mgr, '', status_running=True), \
                mock.patch("cephadm.module.CephadmOrchestrator.lookup_release_name",
                           return_value='foo'), \
                mock.patch("cephadm.module.CephadmOrchestrator.version",
                           new_callable=mock.PropertyMock) as version_mock, \
                mock.patch("cephadm.module.CephadmOrchestrator.get",
                           return_value={
                               # capture fields in both mon and osd maps
                               "require_osd_release": "pacific",
                               "min_mon_release": 16,
                           }):
                version_mock.return_value = 'ceph version 18.2.1 (somehash)'
                assert wait(cephadm_module, cephadm_module.upgrade_start(
                    'to_image', None)) == 'Initiating upgrade to to_image'

                assert wait(cephadm_module, cephadm_module.upgrade_status()
                            ).target_image == 'to_image'

                def _versions_mock(cmd):
                    return json.dumps({
                        'mgr': {
                            'ceph version 1.2.3 (asdf) blah': 1
                        }
                    })

                cephadm_module._mon_command_mock_versions = _versions_mock

                with mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm(json.dumps({
                    'image_id': 'image_id',
                    'repo_digests': ['to_image@repo_digest'],
                    'ceph_version': 'ceph version 18.2.3 (hash)',
                }))):

                    cephadm_module.upgrade._do_upgrade()

                assert cephadm_module.upgrade_status is not None

                with mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm(
                    json.dumps([
                        dict(
                            name=list(cephadm_module.cache.daemons['host1'].keys())[0],
                            style='cephadm',
                            fsid='fsid',
                            container_id='container_id',
                            container_image_name='to_image',
                            container_image_id='image_id',
                            container_image_digests=['to_image@repo_digest'],
                            deployed_by=['to_image@repo_digest'],
                            version='version',
                            state='running',
                        )
                    ])
                )):
                    receive_agent_metadata(cephadm_module, 'host1', ['ls'])
                    receive_agent_metadata(cephadm_module, 'host2', ['ls'])

                with mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm(json.dumps({
                    'image_id': 'image_id',
                    'repo_digests': ['to_image@repo_digest'],
                    'ceph_version': 'ceph version 18.2.3 (hash)',
                }))):
                    cephadm_module.upgrade._do_upgrade()

                _, image, _ = cephadm_module.check_mon_command({
                    'prefix': 'config get',
                    'who': 'global',
                    'key': 'container_image',
                })
                if use_repo_digest:
                    assert image == 'to_image@repo_digest'
                else:
                    assert image == 'to_image'


def test_upgrade_state_null(cephadm_module: CephadmOrchestrator):
    # This test validates https://tracker.ceph.com/issues/47580
    cephadm_module.set_store('upgrade_state', 'null')
    CephadmUpgrade(cephadm_module)
    assert CephadmUpgrade(cephadm_module).upgrade_state is None


@mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm('{}'))
def test_not_enough_mgrs(cephadm_module: CephadmOrchestrator):
    with with_host(cephadm_module, 'host1'):
        with with_service(cephadm_module, ServiceSpec('mgr', placement=PlacementSpec(count=1)), CephadmOrchestrator.apply_mgr, ''):
            with pytest.raises(OrchestratorError):
                wait(cephadm_module, cephadm_module.upgrade_start('image_id', None))


@mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm('{}'))
@mock.patch("cephadm.CephadmOrchestrator.check_mon_command")
def test_enough_mons_for_ok_to_stop(check_mon_command, cephadm_module: CephadmOrchestrator):
    # only 2 monitors, not enough for ok-to-stop to ever pass
    check_mon_command.return_value = (
        0, '{"monmap": {"mons": [{"name": "mon.1"}, {"name": "mon.2"}]}}', '')
    assert not cephadm_module.upgrade._enough_mons_for_ok_to_stop()

    # 3 monitors, ok-to-stop should work fine
    check_mon_command.return_value = (
        0, '{"monmap": {"mons": [{"name": "mon.1"}, {"name": "mon.2"}, {"name": "mon.3"}]}}', '')
    assert cephadm_module.upgrade._enough_mons_for_ok_to_stop()


@mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm('{}'))
@mock.patch("cephadm.module.HostCache.get_daemons_by_service")
@mock.patch("cephadm.CephadmOrchestrator.get")
def test_enough_mds_for_ok_to_stop(get, get_daemons_by_service, cephadm_module: CephadmOrchestrator):
    get.side_effect = [{'filesystems': [{'mdsmap': {'fs_name': 'test', 'max_mds': 1}}]}]
    get_daemons_by_service.side_effect = [[DaemonDescription()]]
    assert not cephadm_module.upgrade._enough_mds_for_ok_to_stop(
        DaemonDescription(daemon_type='mds', daemon_id='test.host1.gfknd', service_name='mds.test'))

    get.side_effect = [{'filesystems': [{'mdsmap': {'fs_name': 'myfs.test', 'max_mds': 2}}]}]
    get_daemons_by_service.side_effect = [[DaemonDescription(), DaemonDescription()]]
    assert not cephadm_module.upgrade._enough_mds_for_ok_to_stop(
        DaemonDescription(daemon_type='mds', daemon_id='myfs.test.host1.gfknd', service_name='mds.myfs.test'))

    get.side_effect = [{'filesystems': [{'mdsmap': {'fs_name': 'myfs.test', 'max_mds': 1}}]}]
    get_daemons_by_service.side_effect = [[DaemonDescription(), DaemonDescription()]]
    assert cephadm_module.upgrade._enough_mds_for_ok_to_stop(
        DaemonDescription(daemon_type='mds', daemon_id='myfs.test.host1.gfknd', service_name='mds.myfs.test'))


@mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm('{}'))
@mock.patch("cephadm.module.HostCache.get_daemons_by_service")
@mock.patch("cephadm.CephadmOrchestrator.get")
def test_enough_mds_for_ok_to_stop_uses_actual_fs_membership(
        get, get_daemons_by_service, cephadm_module: CephadmOrchestrator):
    # A daemon from service mds.fs2 currently holds a rank in fs1 (standby
    # takeover: mds_join_fs is only a preference). The filesystem actually
    # served (fs1, max_mds=2) must be evaluated, not the service's (fs2,
    # max_mds=1).
    fsmap = {'filesystems': [
        {'mdsmap': {'fs_name': 'fs1', 'max_mds': 2,
                    'info': {'gid_1': {'name': 'fs2.host1.gfknd'}}}},
        {'mdsmap': {'fs_name': 'fs2', 'max_mds': 1, 'info': {}}},
    ]}
    get.side_effect = [fsmap]
    get_daemons_by_service.side_effect = [[DaemonDescription(), DaemonDescription()]]
    assert not cephadm_module.upgrade._enough_mds_for_ok_to_stop(
        DaemonDescription(daemon_type='mds', daemon_id='fs2.host1.gfknd',
                          service_name='mds.fs2'))


def _mds_need_upgrade_entries() -> List[Tuple[DaemonDescription, bool]]:
    return [
        (DaemonDescription(
            daemon_type='mds',
            daemon_id=f'cephfs.host{i}.abcdef',
            hostname=f'host{i}',
            container_image_id='old_digest',
            service_name='mds.cephfs',
        ), False)
        for i in range(1, 4)
    ]


@mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm('{}'))
@mock.patch.object(CephadmUpgrade, '_enough_mds_for_ok_to_stop', return_value=True)
@mock.patch.object(CephadmUpgrade, '_wait_for_ok_to_stop', return_value=True)
def test_to_upgrade_batches_mds_when_fail_fs(
        _wait_for_ok_to_stop: mock.MagicMock,
        _enough_mds_for_ok_to_stop: mock.MagicMock,
        cephadm_module: CephadmOrchestrator):
    # https://tracker.ceph.com/issues/78304
    need_upgrade = _mds_need_upgrade_entries()

    cephadm_module.upgrade.upgrade_state = UpgradeState(
        'target_image', 'pid', fail_fs=True)
    cont, to_upgrade = cephadm_module.upgrade._to_upgrade(need_upgrade, 'target_image')
    assert cont
    assert len(to_upgrade) == 3
    assert [d[0].name() for d in to_upgrade] == [d[0].name() for d in need_upgrade]
    _wait_for_ok_to_stop.assert_not_called()

    cephadm_module.upgrade.upgrade_state = UpgradeState(
        'target_image', 'pid', fail_fs=False)
    cont, to_upgrade = cephadm_module.upgrade._to_upgrade(need_upgrade, 'target_image')
    assert cont
    assert len(to_upgrade) == 1
    assert to_upgrade[0][0].name() == need_upgrade[0][0].name()


def _fsmap_two_filesystems():
    # Two multi-rank filesystems. 'up' is non-empty so the fail_fs branch logs
    # and issues 'fs fail'; max_mds > 1 so the max_mds branch issues 'fs set'.
    return {'filesystems': [
        {'id': 1, 'mdsmap': {'fs_name': 'cephfs', 'max_mds': 2, 'flags': 0,
                             'up': {'mds_0': 1, 'mds_1': 2}, 'in': [0, 1],
                             'info': {'gid_1': {'name': 'a', 'state': 'up:active'},
                                      'gid_2': {'name': 'b', 'state': 'up:active'}}}},
        {'id': 2, 'mdsmap': {'fs_name': 'cephfs2', 'max_mds': 2, 'flags': 0,
                             'up': {'mds_0': 3, 'mds_1': 4}, 'in': [0, 1],
                             'info': {'gid_3': {'name': 'c', 'state': 'up:active'},
                                      'gid_4': {'name': 'd', 'state': 'up:active'}}}},
    ]}


@mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm('{}'))
@mock.patch("cephadm.module.CephadmOrchestrator.check_mon_command")
@mock.patch("cephadm.CephadmOrchestrator.get")
def test_prepare_for_mds_upgrade_fail_fs_scopes_to_targeted_fs(
        get, check_mon_command, cephadm_module: CephadmOrchestrator):
    # With fail_fs=true and the upgrade scoped to a single filesystem
    # (need_upgrade only contains cephfs2's MDS), only cephfs2 must be failed.
    check_mon_command.return_value = (0, '', '')
    get.side_effect = lambda what: _fsmap_two_filesystems() if what == "fs_map" else None
    cephadm_module.upgrade.upgrade_state = UpgradeState('target_image', 0, fail_fs=True)

    need_upgrade = [DaemonDescription(daemon_type='mds',
                                      daemon_id='cephfs2.host1.abcde',
                                      service_name='mds.cephfs2')]
    cephadm_module.upgrade._prepare_for_mds_upgrade('18', need_upgrade)

    failed = [c.args[0]['fs_name'] for c in check_mon_command.call_args_list
              if c.args and c.args[0].get('prefix') == 'fs fail']
    assert 'cephfs2' in failed
    assert 'cephfs' not in failed


@mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm('{}'))
@mock.patch("cephadm.module.CephadmOrchestrator.check_mon_command")
@mock.patch("cephadm.CephadmOrchestrator.get")
def test_prepare_for_mds_upgrade_includes_fs_actually_served(
        get, check_mon_command, cephadm_module: CephadmOrchestrator):
    # A daemon from service mds.cephfs2 currently holds a rank in cephfs
    # (standby takeover: mds_join_fs is only a preference). The filesystem
    # it actually serves must be prepared too, not only its service's.
    fsmap = _fsmap_two_filesystems()
    fsmap['filesystems'][0]['mdsmap']['info']['gid_1']['name'] = 'cephfs2.host1.abcde'
    check_mon_command.return_value = (0, '', '')
    get.side_effect = lambda what: fsmap if what == "fs_map" else None
    cephadm_module.upgrade.upgrade_state = UpgradeState('target_image', 0, fail_fs=True)

    need_upgrade = [DaemonDescription(daemon_type='mds',
                                      daemon_id='cephfs2.host1.abcde',
                                      service_name='mds.cephfs2')]
    cephadm_module.upgrade._prepare_for_mds_upgrade('18', need_upgrade)

    failed = [c.args[0]['fs_name'] for c in check_mon_command.call_args_list
              if c.args and c.args[0].get('prefix') == 'fs fail']
    assert 'cephfs' in failed
    assert 'cephfs2' in failed


@mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm('{}'))
@mock.patch("cephadm.module.CephadmOrchestrator.check_mon_command")
@mock.patch("cephadm.CephadmOrchestrator.get")
def test_prepare_for_mds_upgrade_ignores_standby_replay_membership(
        get, check_mon_command, cephadm_module: CephadmOrchestrator):
    # A filesystem with allow_standby_replay can grab a standby from another
    # service's pool as up:standby-replay at any time. That daemon holds no
    # rank there, so the borrowing filesystem must NOT become a preparation
    # target of the borrowed daemon's upgrade (this used to livelock the
    # one-filesystem-at-a-time sequencing by repeatedly disabling
    # standby-replay on the already-restored filesystem).
    fsmap = _fsmap_two_filesystems()
    fsmap['filesystems'][0]['mdsmap']['info']['gid_1'] = {
        'name': 'cephfs2.host1.abcde', 'state': 'up:standby-replay'}
    check_mon_command.return_value = (0, '', '')
    get.side_effect = lambda what: fsmap if what == "fs_map" else None
    cephadm_module.upgrade.upgrade_state = UpgradeState('target_image', 0, fail_fs=True)

    need_upgrade = [DaemonDescription(daemon_type='mds',
                                      daemon_id='cephfs2.host1.abcde',
                                      service_name='mds.cephfs2')]
    cephadm_module.upgrade._prepare_for_mds_upgrade('18', need_upgrade)

    failed = [c.args[0]['fs_name'] for c in check_mon_command.call_args_list
              if c.args and c.args[0].get('prefix') == 'fs fail']
    assert 'cephfs2' in failed
    assert 'cephfs' not in failed


@mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm('{}'))
@mock.patch("cephadm.module.CephadmOrchestrator.check_mon_command")
@mock.patch("cephadm.CephadmOrchestrator.get")
def test_prepare_for_mds_upgrade_max_mds_scopes_to_targeted_fs(
        get, check_mon_command, cephadm_module: CephadmOrchestrator):
    # With fail_fs=false the targeted filesystem is scaled to max_mds 1, and
    # only the targeted filesystem (cephfs2) must be touched.
    check_mon_command.return_value = (0, '', '')
    get.side_effect = lambda what: _fsmap_two_filesystems() if what == "fs_map" else None
    cephadm_module.upgrade.upgrade_state = UpgradeState('target_image', 0, fail_fs=False)

    need_upgrade = [DaemonDescription(daemon_type='mds',
                                      daemon_id='cephfs2.host1.abcde',
                                      service_name='mds.cephfs2')]
    cephadm_module.upgrade._prepare_for_mds_upgrade('18', need_upgrade)

    scaled = [c.args[0]['fs_name'] for c in check_mon_command.call_args_list
              if c.args and c.args[0].get('prefix') == 'fs set'
              and c.args[0].get('var') == 'max_mds']
    assert 'cephfs2' in scaled
    assert 'cephfs' not in scaled


@mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm('{}'))
@mock.patch("cephadm.module.CephadmOrchestrator.check_mon_command")
@mock.patch("cephadm.CephadmOrchestrator.get")
def test_prepare_for_mds_upgrade_all_mds_touches_all_filesystems(
        get, check_mon_command, cephadm_module: CephadmOrchestrator):
    # With --daemon-types mds (or no filter), need_upgrade contains MDS from
    # every filesystem, so all filesystems must still be prepared.
    check_mon_command.return_value = (0, '', '')
    get.side_effect = lambda what: _fsmap_two_filesystems() if what == "fs_map" else None
    cephadm_module.upgrade.upgrade_state = UpgradeState('target_image', 0, fail_fs=True)

    need_upgrade = [
        DaemonDescription(daemon_type='mds', daemon_id='cephfs.host1.aaaaa',
                          service_name='mds.cephfs'),
        DaemonDescription(daemon_type='mds', daemon_id='cephfs2.host1.bbbbb',
                          service_name='mds.cephfs2'),
    ]
    cephadm_module.upgrade._prepare_for_mds_upgrade('18', need_upgrade)

    failed = [c.args[0]['fs_name'] for c in check_mon_command.call_args_list
              if c.args and c.args[0].get('prefix') == 'fs fail']
    assert 'cephfs' in failed
    assert 'cephfs2' in failed


@mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm('{}'))
@mock.patch("cephadm.module.CephadmOrchestrator.check_mon_command")
@mock.patch("cephadm.CephadmOrchestrator.get")
def test_complete_mds_upgrade_rejoins_only_fs_failed_by_upgrade(
        get, check_mon_command, cephadm_module: CephadmOrchestrator):
    # Only filesystems the upgrade itself failed (recorded in
    # fs_failed_for_upgrade) must be set joinable again. A filesystem an admin
    # set NOT_JOINABLE for another reason (here 'cephfs') must be left alone.
    check_mon_command.return_value = (0, '', '')
    get.side_effect = lambda what: _fsmap_two_filesystems() if what == "fs_map" else None
    cephadm_module.upgrade.upgrade_state = UpgradeState(
        'target_image', 0, fail_fs=True, fs_failed_for_upgrade=[2])

    cephadm_module.upgrade._complete_mds_upgrade()

    rejoined = [c.args[0]['fs_name'] for c in check_mon_command.call_args_list
                if c.args and c.args[0].get('prefix') == 'fs set'
                and c.args[0].get('var') == 'joinable']
    assert 'cephfs2' in rejoined
    assert 'cephfs' not in rejoined
    # the tracking list is cleared once completion has run
    assert cephadm_module.upgrade.upgrade_state.fs_failed_for_upgrade == []


@mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm('{}'))
@mock.patch("cephadm.module.CephadmOrchestrator.check_mon_command")
@mock.patch("cephadm.CephadmOrchestrator.get")
def test_complete_mds_upgrade_rejoins_nothing_when_upgrade_failed_no_fs(
        get, check_mon_command, cephadm_module: CephadmOrchestrator):
    # If the upgrade did not fail any filesystem (empty fs_failed_for_upgrade),
    # completion must not set any filesystem joinable.
    check_mon_command.return_value = (0, '', '')
    get.side_effect = lambda what: _fsmap_two_filesystems() if what == "fs_map" else None
    cephadm_module.upgrade.upgrade_state = UpgradeState(
        'target_image', 0, fail_fs=True, fs_failed_for_upgrade=[])

    cephadm_module.upgrade._complete_mds_upgrade()

    rejoined = [c.args[0]['fs_name'] for c in check_mon_command.call_args_list
                if c.args and c.args[0].get('prefix') == 'fs set'
                and c.args[0].get('var') == 'joinable']
    assert rejoined == []


@mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm('{}'))
@mock.patch("cephadm.module.CephadmOrchestrator.check_mon_command")
@mock.patch("cephadm.CephadmOrchestrator.get")
def test_complete_mds_upgrade_scales_up_only_finished_fs(
        get, check_mon_command, cephadm_module: CephadmOrchestrator):
    # With fail_fs=false, filesystems are scaled down to max_mds 1 during the
    # upgrade (recorded in fs_original_max_mds by fscid). When completion is
    # invoked for a single finished filesystem (fs_names given), only that
    # filesystem must be scaled back up; the other entries must be retained
    # for later restoration.
    check_mon_command.return_value = (0, '', '')
    get.side_effect = lambda what: _fsmap_two_filesystems() if what == "fs_map" else None
    cephadm_module.upgrade.upgrade_state = UpgradeState(
        'target_image', 0, fail_fs=False,
        fs_original_max_mds={1: 2, 2: 2})

    cephadm_module.upgrade._complete_mds_upgrade(fs_names=['cephfs'])

    scaled = [c.args[0]['fs_name'] for c in check_mon_command.call_args_list
              if c.args and c.args[0].get('prefix') == 'fs set'
              and c.args[0].get('var') == 'max_mds']
    assert scaled == ['cephfs']
    # cephfs2 (fscid 2) is still being upgraded: its entry must remain
    assert cephadm_module.upgrade.upgrade_state.fs_original_max_mds == {2: 2}


@pytest.mark.parametrize("current_version, use_tags, show_all_versions, tags, result",
                         [
                             # several candidate versions (from different major versions)
                             (
                                 (16, 1, '16.1.0'),
                                 False,  # use_tags
                                 False,  # show_all_versions
                                 [
                                     'v17.1.0',
                                     'v16.2.7',
                                     'v16.2.6',
                                     'v16.2.5',
                                     'v16.1.4',
                                     'v16.1.3',
                                     'v15.2.0',
                                 ],
                                 ['17.1.0', '16.2.7', '16.2.6', '16.2.5', '16.1.4', '16.1.3']
                             ),
                             # candidate minor versions are available
                             (
                                 (16, 1, '16.1.0'),
                                 False,  # use_tags
                                 False,  # show_all_versions
                                 [
                                     'v16.2.2',
                                     'v16.2.1',
                                     'v16.1.6',
                                 ],
                                 ['16.2.2', '16.2.1', '16.1.6']
                             ),
                             # all versions are less than the current version
                             (
                                 (17, 2, '17.2.0'),
                                 False,  # use_tags
                                 False,  # show_all_versions
                                 [
                                     'v17.1.0',
                                     'v16.2.7',
                                     'v16.2.6',
                                 ],
                                 []
                             ),
                             # show all versions (regardless of the current version)
                             (
                                 (16, 1, '16.1.0'),
                                 False,  # use_tags
                                 True,   # show_all_versions
                                 [
                                     'v17.1.0',
                                     'v16.2.7',
                                     'v16.2.6',
                                     'v15.1.0',
                                     'v14.2.0',
                                 ],
                                 ['17.1.0', '16.2.7', '16.2.6', '15.1.0', '14.2.0']
                             ),
                             # show all tags (regardless of the current version and show_all_versions flag)
                             (
                                 (16, 1, '16.1.0'),
                                 True,   # use_tags
                                 False,  # show_all_versions
                                 [
                                     'v17.1.0',
                                     'v16.2.7',
                                     'v16.2.6',
                                     'v16.2.5',
                                     'v16.1.4',
                                     'v16.1.3',
                                     'v15.2.0',
                                 ],
                                 ['v15.2.0', 'v16.1.3', 'v16.1.4', 'v16.2.5',
                                     'v16.2.6', 'v16.2.7', 'v17.1.0']
                             ),
                         ])
@mock.patch("cephadm.serve.CephadmServe._run_cephadm", _run_cephadm('{}'))
def test_upgrade_ls(current_version, use_tags, show_all_versions, tags, result, cephadm_module: CephadmOrchestrator):
    with mock.patch('cephadm.upgrade.Registry.get_tags', return_value=tags):
        with mock.patch('cephadm.upgrade.CephadmUpgrade._get_current_version', return_value=current_version):
            out = cephadm_module.upgrade.upgrade_ls(None, use_tags, show_all_versions)
            if use_tags:
                assert out['tags'] == result
            else:
                assert out['versions'] == result


@pytest.mark.parametrize(
    "upgraded, not_upgraded, daemon_types, hosts, services, should_block",
    # [ ([(type, host, id), ... ], [...], [daemon types], [hosts], [services], True/False), ... ]
    [
        (  # valid, upgrade mgr daemons
            [],
            [('mgr', 'a', 'a.x'), ('mon', 'a', 'a')],
            ['mgr'],
            None,
            None,
            False
        ),
        (  # invalid, can't upgrade mons until mgr is upgraded
            [],
            [('mgr', 'a', 'a.x'), ('mon', 'a', 'a')],
            ['mon'],
            None,
            None,
            True
        ),
        (  # invalid, can't upgrade mon service until all mgr daemons are upgraded
            [],
            [('mgr', 'a', 'a.x'), ('mon', 'a', 'a')],
            None,
            None,
            ['mon'],
            True
        ),
        (  # valid, upgrade mgr service
            [],
            [('mgr', 'a', 'a.x'), ('mon', 'a', 'a')],
            None,
            None,
            ['mgr'],
            False
        ),
        (  # valid, mgr is already upgraded so can upgrade mons
            [('mgr', 'a', 'a.x')],
            [('mon', 'a', 'a')],
            ['mon'],
            None,
            None,
            False
        ),
        (  # invalid, can't upgrade all daemons on b b/c un-upgraded mgr on a
            [],
            [('mgr', 'b', 'b.y'), ('mon', 'a', 'a')],
            None,
            ['a'],
            None,
            True
        ),
        (  # valid, only daemon on b is a mgr
            [],
            [('mgr', 'a', 'a.x'), ('mgr', 'b', 'b.y'), ('mon', 'a', 'a')],
            None,
            ['b'],
            None,
            False
        ),
        (  # invalid, can't upgrade mon on a while mgr on b is un-upgraded
            [],
            [('mgr', 'a', 'a.x'), ('mgr', 'b', 'b.y'), ('mon', 'a', 'a')],
            None,
            ['a'],
            None,
            True
        ),
        (  # valid, only upgrading the mgr on a
            [],
            [('mgr', 'a', 'a.x'), ('mgr', 'b', 'b.y'), ('mon', 'a', 'a')],
            ['mgr'],
            ['a'],
            None,
            False
        ),
        (  # valid, mgr daemon not on b are upgraded
            [('mgr', 'a', 'a.x')],
            [('mgr', 'b', 'b.y'), ('mon', 'a', 'a')],
            None,
            ['b'],
            None,
            False
        ),
        (  # valid, all the necessary hosts are covered, mgr on c is already upgraded
            [('mgr', 'c', 'c.z')],
            [('mgr', 'a', 'a.x'), ('mgr', 'b', 'b.y'), ('mon', 'a', 'a'), ('osd', 'c', '0')],
            None,
            ['a', 'b'],
            None,
            False
        ),
        (  # invalid, can't upgrade mon on a while mgr on b is un-upgraded
            [],
            [('mgr', 'a', 'a.x'), ('mgr', 'b', 'b.y'), ('mon', 'a', 'a')],
            ['mgr', 'mon'],
            ['a'],
            None,
            True
        ),
        (  # valid, only mon not on "b" is upgraded already. Case hit while making teuthology test
            [('mon', 'a', 'a')],
            [('mon', 'b', 'x'), ('mon', 'b', 'y'), ('osd', 'a', '1'), ('osd', 'b', '2')],
            ['mon', 'osd'],
            ['b'],
            None,
            False
        ),
    ]
)
@mock.patch("cephadm.module.HostCache.get_daemons")
@mock.patch("cephadm.serve.CephadmServe._get_container_image_info")
@mock.patch('cephadm.module.SpecStore.__getitem__')
def test_staggered_upgrade_validation(
        get_spec,
        get_image_info,
        get_daemons,
        upgraded: List[Tuple[str, str, str]],
        not_upgraded: List[Tuple[str, str, str, str]],
        daemon_types: Optional[str],
        hosts: Optional[str],
        services: Optional[str],
        should_block: bool,
        cephadm_module: CephadmOrchestrator,
):
    def to_dds(ts: List[Tuple[str, str]], upgraded: bool) -> List[DaemonDescription]:
        dds = []
        digest = 'new_image@repo_digest' if upgraded else 'old_image@repo_digest'
        for t in ts:
            dds.append(DaemonDescription(daemon_type=t[0],
                                         hostname=t[1],
                                         daemon_id=t[2],
                                         container_image_digests=[digest],
                                         deployed_by=[digest],))
        return dds
    get_daemons.return_value = to_dds(upgraded, True) + to_dds(not_upgraded, False)
    get_image_info.side_effect = async_side_effect(
        ('new_id', 'ceph version 99.99.99 (hash)', ['new_image@repo_digest']))

    class FakeSpecDesc():
        def __init__(self, spec):
            self.spec = spec

    def _get_spec(s):
        return FakeSpecDesc(ServiceSpec(s))

    get_spec.side_effect = _get_spec
    if should_block:
        with pytest.raises(OrchestratorError):
            cephadm_module.upgrade._validate_upgrade_filters(
                'new_image_name', daemon_types, hosts, services)
    else:
        cephadm_module.upgrade._validate_upgrade_filters(
            'new_image_name', daemon_types, hosts, services)


def _mds_entries_for_services(*service_names):
    # Build (DaemonDescription, bool) entries like _detect_need_upgrade returns,
    # one MDS per given service name (service_name is 'mds.<fs>').
    entries = []
    for i, svc in enumerate(service_names):
        fs = svc[len('mds.'):]
        entries.append(
            (DaemonDescription(daemon_type='mds',
                               daemon_id=f'{fs}.host{i}.aaaaa',
                               service_name=svc), False))
    return entries


def test_restrict_mds_need_upgrade_to_one_fs_picks_single_fs(
        cephadm_module: CephadmOrchestrator):
    # MDS from three filesystems -> only one filesystem's MDS are kept.
    need_upgrade = _mds_entries_for_services(
        'mds.cephfs2', 'mds.cephfs', 'mds.cephfs', 'mds.cephfs3')
    restricted = cephadm_module.upgrade._restrict_mds_need_upgrade_to_one_fs(need_upgrade)
    fs_names = {d.service_name() for d, _ in restricted}
    assert fs_names == {'mds.cephfs'}, fs_names


def test_restrict_mds_need_upgrade_to_one_fs_is_deterministic(
        cephadm_module: CephadmOrchestrator):
    # Selection is the lowest (sorted) service name, regardless of input order.
    a = cephadm_module.upgrade._restrict_mds_need_upgrade_to_one_fs(
        _mds_entries_for_services('mds.b', 'mds.a', 'mds.c'))
    b = cephadm_module.upgrade._restrict_mds_need_upgrade_to_one_fs(
        _mds_entries_for_services('mds.c', 'mds.b', 'mds.a'))
    assert {d.service_name() for d, _ in a} == {'mds.a'}
    assert {d.service_name() for d, _ in b} == {'mds.a'}


def test_restrict_mds_need_upgrade_to_one_fs_sequences_across_passes(
        cephadm_module: CephadmOrchestrator):
    # Simulate successive serve() passes: once a filesystem's MDS are upgraded
    # they drop out of need_upgrade and the next filesystem is selected.
    selected = []
    remaining = ['mds.cephfs', 'mds.cephfs2', 'mds.cephfs3']
    # one MDS per fs for simplicity
    while remaining:
        entries = _mds_entries_for_services(*remaining)
        restricted = cephadm_module.upgrade._restrict_mds_need_upgrade_to_one_fs(entries)
        fs_names = {d.service_name() for d, _ in restricted}
        assert len(fs_names) == 1
        picked = fs_names.pop()
        selected.append(picked)
        remaining.remove(picked)
    assert selected == ['mds.cephfs', 'mds.cephfs2', 'mds.cephfs3']


def test_restrict_mds_need_upgrade_to_one_fs_handles_multi_part_fs_names(
        cephadm_module: CephadmOrchestrator):
    # Filesystem names may themselves contain dots (service 'mds.my.fs');
    # everything after the 'mds.' prefix is the filesystem grouping key.
    need_upgrade = _mds_entries_for_services('mds.my.fs', 'mds.my.fs', 'mds.other')
    restricted = cephadm_module.upgrade._restrict_mds_need_upgrade_to_one_fs(need_upgrade)
    fs_names = {d.service_name() for d, _ in restricted}
    # 'mds.my.fs' sorts before 'mds.other'
    assert fs_names == {'mds.my.fs'}, fs_names
    assert len(restricted) == 2


def test_upgrade_state_image_mirror_roundtrip():
    u = UpgradeState('target', 'pid', image_mirror_done=True)
    restored = UpgradeState.from_json(u.to_json())
    assert restored
    assert restored.image_mirror_done is True


def test_host_has_target_image_matches_image_id():
    upgrade = CephadmUpgrade.__new__(CephadmUpgrade)
    upgrade.upgrade_state = UpgradeState(
        '192.168.100.254:5000/ceph/ceph:main2.0',
        'pid',
        target_id='acf49863d4a5ce75d68464d3924f3d3f8a7af21544e7bd9430805f04d70eed8c',
        target_digests=['192.168.100.254:5000/ceph/ceph@sha256:abc'],
    )
    assert upgrade._host_has_target_image(
        {'image_id': 'acf49863d4a5ce75d68464d3924f3d3f8a7af21544e7bd9430805f04d70eed8c',
         'repo_digests': []},
        ['192.168.100.254:5000/ceph/ceph@sha256:abc'],
    )


@mock.patch.object(CephadmUpgrade, '_update_upgrade_progress')
@mock.patch.object(CephadmUpgrade, '_get_upgrade_scope_hosts', return_value=['host1'])
@mock.patch.object(CephadmUpgrade, '_pre_distribute_upgrade_images', return_value=True)
def test_do_upgrade_calls_image_mirror_before_daemons(
    mirror_mock: mock.MagicMock,
    _get_upgrade_scope_hosts: mock.MagicMock,
    _update_upgrade_progress: mock.MagicMock,
    cephadm_module: CephadmOrchestrator,
):
    cephadm_module.upgrade_image_mirror_method = UPGRADE_IMAGE_MIRROR_METHOD_REGISTRY
    cephadm_module.upgrade.upgrade_state = UpgradeState(
        'target_image',
        'pid',
        target_id='image_id',
        target_digests=['target_image@digest'],
        target_version='19.3.0-0',
        image_mirror_done=False,
    )
    upgrade_daemon = _upgrade_test_daemon()
    with mock.patch.object(CephadmUpgrade, '_detect_need_upgrade', return_value=(False, [], [], 0)), \
            mock.patch.object(CephadmUpgrade, '_to_upgrade', return_value=(True, [])), \
            mock.patch.object(CephadmUpgrade, '_get_filtered_daemons', return_value=[upgrade_daemon]), \
            mock.patch.object(CephadmUpgrade, 'get_distinct_container_image_settings', return_value={}), \
            mock.patch("cephadm.module.CephadmOrchestrator.lookup_release_name", return_value='tentacle'), \
            mock.patch("cephadm.module.CephadmOrchestrator.check_mon_command", return_value=(0, '{}', '')), \
            mock.patch("cephadm.module.CephadmOrchestrator.get", return_value={
                'min_mon_release': 19,
                'require_osd_release': 'tentacle',
                'have_local_config_map': True,
            }), \
            mock.patch(
                "cephadm.module.CephadmOrchestrator.version",
                new_callable=mock.PropertyMock,
                return_value='ceph version 19.3.0-0 (hash)'), \
            mock.patch("cephadm.module.HostCache.get_daemons", return_value=[upgrade_daemon]):
        cephadm_module.upgrade._do_upgrade()
    mirror_mock.assert_called_once()


@mock.patch.object(CephadmUpgrade, '_update_upgrade_progress')
@mock.patch.object(CephadmUpgrade, '_pre_distribute_upgrade_images')
def test_do_upgrade_skips_image_mirror_when_done(
    mirror_mock: mock.MagicMock,
    _update_upgrade_progress: mock.MagicMock,
    cephadm_module: CephadmOrchestrator,
):
    cephadm_module.upgrade_image_mirror_method = UPGRADE_IMAGE_MIRROR_METHOD_REGISTRY
    cephadm_module.upgrade.upgrade_state = UpgradeState(
        'target_image',
        'pid',
        target_id='image_id',
        target_digests=['target_image@digest'],
        target_version='19.3.0-0',
        image_mirror_done=True,
    )
    upgrade_daemon = _upgrade_test_daemon()
    with mock.patch.object(CephadmUpgrade, '_detect_need_upgrade', return_value=(False, [], [], 0)), \
            mock.patch.object(CephadmUpgrade, '_to_upgrade', return_value=(True, [])), \
            mock.patch.object(CephadmUpgrade, 'get_distinct_container_image_settings', return_value={}), \
            mock.patch("cephadm.module.CephadmOrchestrator.lookup_release_name", return_value='tentacle'), \
            mock.patch("cephadm.module.CephadmOrchestrator.check_mon_command", return_value=(0, '{}', '')), \
            mock.patch("cephadm.module.CephadmOrchestrator.get", return_value={
                'min_mon_release': 19,
                'require_osd_release': 'tentacle',
                'have_local_config_map': True,
            }), \
            mock.patch(
                "cephadm.module.CephadmOrchestrator.version",
                new_callable=mock.PropertyMock,
                return_value='ceph version 19.3.0-0 (hash)'), \
            mock.patch("cephadm.module.HostCache.get_daemons", return_value=[upgrade_daemon]):
        cephadm_module.upgrade._do_upgrade()
    mirror_mock.assert_not_called()


@mock.patch.object(CephadmUpgrade, '_update_upgrade_progress')
@mock.patch.object(CephadmUpgrade, '_pre_distribute_upgrade_images')
def test_do_upgrade_skips_image_mirror_when_method_disabled(
    mirror_mock: mock.MagicMock,
    _update_upgrade_progress: mock.MagicMock,
    cephadm_module: CephadmOrchestrator,
):
    for disabled in ('', 'none', 'NONE', ' None '):
        cephadm_module.upgrade_image_mirror_method = disabled
        cephadm_module.upgrade.upgrade_state = UpgradeState(
            'target_image',
            'pid',
            target_id='image_id',
            target_digests=['target_image@digest'],
            target_version='19.3.0-0',
            image_mirror_done=False,
        )
        upgrade_daemon = _upgrade_test_daemon()
        with mock.patch.object(CephadmUpgrade, '_detect_need_upgrade', return_value=(False, [], [], 0)), \
                mock.patch.object(CephadmUpgrade, '_to_upgrade', return_value=(True, [])), \
                mock.patch.object(CephadmUpgrade, 'get_distinct_container_image_settings', return_value={}), \
                mock.patch("cephadm.module.CephadmOrchestrator.lookup_release_name", return_value='tentacle'), \
                mock.patch("cephadm.module.CephadmOrchestrator.check_mon_command", return_value=(0, '{}', '')), \
                mock.patch("cephadm.module.CephadmOrchestrator.get", return_value={
                    'min_mon_release': 19,
                    'require_osd_release': 'tentacle',
                    'have_local_config_map': True,
                }), \
                mock.patch(
                    "cephadm.module.CephadmOrchestrator.version",
                    new_callable=mock.PropertyMock,
                    return_value='ceph version 19.3.0-0 (hash)'), \
                mock.patch("cephadm.module.HostCache.get_daemons", return_value=[upgrade_daemon]):
            cephadm_module.upgrade._do_upgrade()
        assert mirror_mock.call_count == 0
    mirror_mock.assert_not_called()


@mock.patch.object(CephadmUpgrade, '_pre_pull_image_on_hosts', return_value=True)
def test_pre_distribute_upgrade_images_uses_registry(
    registry_mock: mock.MagicMock,
    cephadm_module: CephadmOrchestrator,
):
    cephadm_module.upgrade_image_mirror_method = UPGRADE_IMAGE_MIRROR_METHOD_REGISTRY
    assert cephadm_module.upgrade._pre_distribute_upgrade_images(
        'target_image', ['target_image@digest'], ['host1']) is True
    registry_mock.assert_called_once_with(
        'target_image', ['target_image@digest'], ['host1'])


@mock.patch.object(CephadmUpgrade, '_pre_pull_image_on_hosts')
def test_pre_distribute_upgrade_images_noop_when_disabled(
    registry_mock: mock.MagicMock,
    cephadm_module: CephadmOrchestrator,
):
    for disabled in ('', 'none'):
        cephadm_module.upgrade_image_mirror_method = disabled
        assert cephadm_module.upgrade._pre_distribute_upgrade_images(
            'target_image', ['target_image@digest'], ['host1']) is True
    registry_mock.assert_not_called()


def test_pre_distribute_upgrade_images_rejects_unknown_method(
    cephadm_module: CephadmOrchestrator,
):
    cephadm_module.upgrade.upgrade_state = UpgradeState('target_image', 'pid')
    cephadm_module.upgrade_image_mirror_method = 'local_http'
    assert cephadm_module.upgrade._pre_distribute_upgrade_images(
        'target_image', ['target_image@digest'], ['host1']) is False
    assert 'UPGRADE_FAILED_PULL' in cephadm_module.health_checks
    detail = ' '.join(cephadm_module.health_checks['UPGRADE_FAILED_PULL']['detail'])
    assert 'local_http' in detail


def _registry_prepull_upgrade_state(cephadm_module: CephadmOrchestrator):
    cephadm_module.upgrade.upgrade_state = UpgradeState(
        'quay.io/ceph/ceph:vtest',
        'pid',
        target_digests=['quay.io/ceph/ceph@sha256:targetdigest'],
        target_id='targetdigest',
        target_version='19.2.0',
    )


def _inspect_or_pull(present_hosts, fail_pull_hosts):
    pulled: set = set()

    async def fake_run(self, host, entity, command, args, image=None,
                       no_fsid=None, error_ok=None, **kwargs):
        if command == 'inspect-image':
            if host in present_hosts or host in pulled:
                return (
                    ['{"repo_digests": ["quay.io/ceph/ceph@sha256:targetdigest"]}'],
                    [], 0)
            return (['{"repo_digests": ["sha256:other"]}'], [], 0)
        if host in fail_pull_hosts:
            return ([], ['no space left on device'], 1)
        pulled.add(host)
        return (
            ['{"repo_digests": ["quay.io/ceph/ceph@sha256:targetdigest"]}'],
            [], 0)
    return fake_run


@mock.patch.object(CephadmUpgrade, '_registry_login_if_needed', new_callable=mock.AsyncMock)
def test_registry_pre_pull_success_on_all_hosts(
    _registry_login: mock.AsyncMock,
    cephadm_module: CephadmOrchestrator,
):
    _registry_prepull_upgrade_state(cephadm_module)
    cephadm_module.upgrade_image_mirror_max_parallel = 8
    with mock.patch("cephadm.serve.CephadmServe._run_cephadm",
                    new=_inspect_or_pull(set(), set())):
        ok = cephadm_module.wait_async(
            cephadm_module.upgrade._pre_pull_image_on_hosts_async(
                'quay.io/ceph/ceph:vtest',
                ['quay.io/ceph/ceph@sha256:targetdigest'],
                ['h1', 'h2', 'h3'],
            ))
    assert ok is True
    assert cephadm_module.upgrade.upgrade_state.image_mirror_done is True
    assert 'UPGRADE_FAILED_PULL' not in cephadm_module.health_checks


@mock.patch.object(CephadmUpgrade, '_registry_login_if_needed', new_callable=mock.AsyncMock)
def test_registry_pre_pull_skips_hosts_that_already_have_image(
    _registry_login: mock.AsyncMock,
    cephadm_module: CephadmOrchestrator,
):
    _registry_prepull_upgrade_state(cephadm_module)
    cephadm_module.upgrade_image_mirror_max_parallel = 8
    pulled = []

    async def fake_run(self, host, entity, command, args, image=None,
                       no_fsid=None, error_ok=None, **kwargs):
        if command == 'inspect-image':
            digest = (
                'quay.io/ceph/ceph@sha256:targetdigest'
                if host == 'h2' or host in pulled else 'sha256:other')
            return ([f'{{"repo_digests": ["{digest}"]}}'], [], 0)
        pulled.append(host)
        return (
            ['{"repo_digests": ["quay.io/ceph/ceph@sha256:targetdigest"]}'],
            [], 0)

    with mock.patch("cephadm.serve.CephadmServe._run_cephadm", new=fake_run):
        ok = cephadm_module.wait_async(
            cephadm_module.upgrade._pre_pull_image_on_hosts_async(
                'quay.io/ceph/ceph:vtest',
                ['quay.io/ceph/ceph@sha256:targetdigest'],
                ['h1', 'h2', 'h3'],
            ))
    assert ok is True
    assert 'h2' not in pulled
    assert sorted(pulled) == ['h1', 'h3']


@mock.patch.object(CephadmUpgrade, '_registry_login_if_needed', new_callable=mock.AsyncMock)
def test_registry_pre_pull_failure_pauses_and_reports_host(
    _registry_login: mock.AsyncMock,
    cephadm_module: CephadmOrchestrator,
):
    _registry_prepull_upgrade_state(cephadm_module)
    cephadm_module.upgrade_image_mirror_max_parallel = 8
    with mock.patch("cephadm.serve.CephadmServe._run_cephadm",
                    new=_inspect_or_pull(set(), {'h2'})):
        ok = cephadm_module.wait_async(
            cephadm_module.upgrade._pre_pull_image_on_hosts_async(
                'quay.io/ceph/ceph:vtest',
                ['quay.io/ceph/ceph@sha256:targetdigest'],
                ['h1', 'h2', 'h3'],
            ))
    assert ok is False
    assert cephadm_module.upgrade.upgrade_state.paused
    assert 'UPGRADE_FAILED_PULL' in cephadm_module.health_checks
    detail = ' '.join(cephadm_module.health_checks['UPGRADE_FAILED_PULL']['detail'])
    assert 'h2' in detail
    assert 'no space left on device' in detail


_PULL_INFO_JSON = json.dumps({
    'image_id': 'sha256:targetdigest',
    'repo_digests': ['quay.io/ceph/ceph@sha256:targetdigest'],
    'ceph_version': 'ceph version 19.2.0 (abc) squid (stable)',
})


@mock.patch.object(CephadmUpgrade, '_registry_login_if_needed', new_callable=mock.AsyncMock)
def test_registry_pre_pull_discovers_metadata_from_parallel_pulls(
    _registry_login: mock.AsyncMock,
    cephadm_module: CephadmOrchestrator,
):
    """When digests/version are unknown, learn them from parallel pulls."""
    cephadm_module.upgrade.upgrade_state = UpgradeState(
        'quay.io/ceph/ceph:vtest',
        'pid',
    )
    cephadm_module.upgrade_image_mirror_max_parallel = 8
    pulled: List[str] = []

    async def fake_run(self, host, entity, command, args, image=None,
                       no_fsid=None, error_ok=None, **kwargs):
        assert command == 'pull'
        pulled.append(host)
        return ([_PULL_INFO_JSON], [], 0)

    with mock.patch("cephadm.serve.CephadmServe._run_cephadm", new=fake_run):
        ok = cephadm_module.wait_async(
            cephadm_module.upgrade._pre_pull_image_on_hosts_async(
                'quay.io/ceph/ceph:vtest',
                [],
                ['h1', 'h2', 'h3'],
            ))
    assert ok is True
    assert sorted(pulled) == ['h1', 'h2', 'h3']
    st = cephadm_module.upgrade.upgrade_state
    assert st.image_mirror_done is True
    assert st.target_id == 'sha256:targetdigest'
    assert st.target_digests == ['quay.io/ceph/ceph@sha256:targetdigest']
    assert st.target_version == '19.2.0'
    assert 'UPGRADE_FAILED_PULL' not in cephadm_module.health_checks


@mock.patch.object(CephadmUpgrade, '_update_upgrade_progress')
@mock.patch.object(CephadmUpgrade, '_get_upgrade_scope_hosts', return_value=['h1', 'h2'])
@mock.patch.object(CephadmUpgrade, '_pre_pull_image_on_hosts')
def test_do_upgrade_registry_discovers_without_serial_first_pull(
    pre_pull_mock: mock.MagicMock,
    _get_upgrade_scope_hosts: mock.MagicMock,
    _update_upgrade_progress: mock.MagicMock,
    cephadm_module: CephadmOrchestrator,
):
    """Registry mirror with no target metadata must not call serial First pull."""
    cephadm_module.upgrade_image_mirror_method = UPGRADE_IMAGE_MIRROR_METHOD_REGISTRY
    cephadm_module.upgrade.upgrade_state = UpgradeState(
        'quay.io/ceph/ceph:vtest',
        'pid',
        image_mirror_done=False,
    )

    learned = {}

    def _fake_pre_pull(target_image, target_digests, hosts):
        st = cephadm_module.upgrade.upgrade_state
        learned['state'] = st
        assert st is not None
        st.target_id = 'sha256:targetdigest'
        st.target_digests = ['quay.io/ceph/ceph@sha256:targetdigest']
        st.target_version = '19.2.0'
        st.image_mirror_done = True
        return True

    pre_pull_mock.side_effect = _fake_pre_pull
    upgrade_daemon = _upgrade_test_daemon()
    first_pull = mock.AsyncMock(
        side_effect=AssertionError('serial first pull must not run'))

    with mock.patch.object(CephadmUpgrade, '_detect_need_upgrade', return_value=(False, [], [], 0)), \
            mock.patch.object(CephadmUpgrade, '_to_upgrade', return_value=(True, [])), \
            mock.patch.object(CephadmUpgrade, '_get_filtered_daemons', return_value=[upgrade_daemon]), \
            mock.patch.object(CephadmUpgrade, '_pre_distribute_upgrade_images') as mirror_mock, \
            mock.patch.object(CephadmUpgrade, 'get_distinct_container_image_settings', return_value={}), \
            mock.patch("cephadm.serve.CephadmServe._get_container_image_info", new=first_pull), \
            mock.patch("cephadm.module.CephadmOrchestrator.lookup_release_name", return_value='tentacle'), \
            mock.patch("cephadm.module.CephadmOrchestrator.check_mon_command", return_value=(0, '{}', '')), \
            mock.patch("cephadm.module.CephadmOrchestrator.get", return_value={
                'min_mon_release': 19,
                'require_osd_release': 'tentacle',
                'have_local_config_map': True,
            }), \
            mock.patch(
                "cephadm.module.CephadmOrchestrator.version",
                new_callable=mock.PropertyMock,
                return_value='ceph version 19.2.0-0 (hash)'), \
            mock.patch("cephadm.module.HostCache.get_daemons", return_value=[upgrade_daemon]):
        cephadm_module.upgrade._do_upgrade()

    pre_pull_mock.assert_called_once()
    mirror_mock.assert_not_called()
    first_pull.assert_not_called()
    assert learned['state'].target_version == '19.2.0'
