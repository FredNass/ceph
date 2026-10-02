"""
Staged switch: upgrade a group of daemons with one restart each.

The regular upgrade path takes a daemon (or a group of daemons, e.g. the
MDS of a failed filesystem) out of service and *then* redeploys it, one
``cephadm deploy`` call at a time. Each call spends most of its time on
work that does not need the daemon to be down: starting cephadm on the
host, a throwaway container to look up uid/gid in the target image,
writing unit files, daemon-reload. The staged switch moves all of that
out of the outage window:

  stage    ``cephadm deploy --stage`` on every daemon of the group, hosts
           in parallel, while the daemons still serve. Config, keyring and
           unit.*.staged are written next to the live files, the target
           image is executed once. Anything that can go wrong with the
           image goes wrong here.
  down     the policy takes the group out of service (MDS: ``fs fail``,
           optionally preceded by a journal flush of the active ranks;
           OSD: ``osd set-group noout`` on the group).
  switch   ``cephadm switch-staged`` on every daemon, hosts in parallel:
           one container stop/start each.
  verify   the policy asks the monitors - not cephadm's daemon cache -
           whether every daemon is back on the target version.
  restore  the policy puts the group back into service (MDS: ``fs set
           joinable true``; OSD: ``osd unset-group noout``), and cephadm's
           cache is refreshed for the hosts involved.

If staging fails nothing has been restarted and the upgrade pauses with
UPGRADE_STAGE_FAILED. If the switch or the verification fails, a policy
with ``rollback_on_failure`` (MDS) has every daemon switched back to its
previous unit files and the group restored on the previous release; one
without (OSD: a store a newer ceph-osd has opened is not to be reopened
by the previous release) leaves the daemons as they are and the upgrade
resumes at the same phase once the admin has dealt with them. Either way
the upgrade pauses with UPGRADE_SWITCH_FAILED. Progress is persisted in
UpgradeState.staged_switch so a mgr failover resumes at the right phase;
every phase is idempotent.

A policy can also answer "not now" (StagedSwitchNotReady): nothing is
staged, the upgrade is not paused, and the next serve() pass asks again.
This is how the OSD policy waits for the PGs to recover between groups.

The runner is daemon-type agnostic. What a "group" is, how it is taken
down, verified and restored is a StagedSwitchPolicy. On reef only
OsdStagedSwitchPolicy is shipped here (every OSD still to upgrade under
one CRUSH bucket of a given type, when ``osd ok-to-stop`` says every PG
stays active); the staged MDS switch of this backport lives in
CephadmUpgrade._staged_mds_upgrade.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Set, Tuple, Type

from orchestrator import DaemonDescription, OrchestratorError, daemon_type_to_service
from cephadm.serve import CephadmServe
from cephadm.services.cephadmservice import CephadmDaemonDeploySpec
from cephadm.utils import name_to_config_section

if TYPE_CHECKING:
    from cephadm.upgrade import CephadmUpgrade

logger = logging.getLogger(__name__)

PHASE_STAGING = 'staging'
PHASE_STAGED = 'staged'
PHASE_DOWN = 'down'
PHASE_SWITCHING = 'switching'
PHASE_SWITCHED = 'switched'
PHASE_ROLLING_BACK = 'rolling_back'


class StagedSwitchNotReady(Exception):
    """Raised by a policy when there is nothing it can safely switch *right
    now* (e.g. no CRUSH bucket of OSDs is ok-to-stop while the PGs of the
    previous group recover). Not an error: the runner leaves the upgrade
    running and the next serve() pass asks again."""


class StagedGroup:
    """The daemons switched together, and what the policy needs to know
    about them. `data` and `snapshot` are persisted as JSON."""

    def __init__(self, key: str, label: str, daemons: List[DaemonDescription],
                 data: Optional[Dict[str, Any]] = None) -> None:
        self.key = key
        self.label = label
        self.daemons = daemons
        self.data: Dict[str, Any] = data or {}
        self.snapshot: Dict[str, Any] = {}

    @property
    def names(self) -> List[str]:
        return [d.name() for d in self.daemons]

    @property
    def hosts(self) -> List[str]:
        return sorted({d.hostname for d in self.daemons if d.hostname})


class StagedSwitchPolicy(ABC):
    """What the runner needs to know about one daemon type."""

    daemon_type: str = ''
    # Whether a switch that does not complete (switch-staged failed, or the
    # group is not back on the target version in time) is undone by putting
    # the previous unit files back. Right for daemons that keep no local
    # state the new release may have touched (MDS); wrong for OSDs, whose
    # stores a newer ceph-osd may have upgraded on boot. With False the
    # runner pauses the upgrade and keeps its state, so `ceph orch upgrade
    # resume` picks the group up again at the same phase.
    rollback_on_failure: bool = True

    def __init__(self, upgrade: 'CephadmUpgrade') -> None:
        self.upgrade = upgrade
        self.mgr = upgrade.mgr

    def verify_timeout(self) -> int:
        """Seconds to wait for the monitors to see the whole group back
        before giving up on the switch."""
        return int(self.mgr.upgrade_staged_switch_timeout)

    def enabled(self) -> bool:
        """Whether the staged switch can be used for this type right now
        (e.g. the MDS policy needs fail_fs). Log the reason when not."""
        return True

    @abstractmethod
    def groups(self, need_upgrade: List[DaemonDescription]) -> List[StagedGroup]:
        """Split the daemons still to be upgraded into ordered groups that
        can be taken down together. The runner handles the first one.

        An empty list means the policy has nothing to handle and the caller
        upgrades these daemons the regular way. Raise StagedSwitchNotReady
        when there is something to handle but it cannot be done safely right
        now; raise OrchestratorError for a configuration problem (the
        upgrade is then paused with UPGRADE_STAGE_FAILED)."""

    def preconditions(self, group: StagedGroup) -> Optional[str]:
        """A reason not to start on this group, or None."""
        return None

    @abstractmethod
    def take_down(self, group: StagedGroup) -> None:
        """Take the group out of service. Must be safe to call again on a
        group that is already down (mgr failover). May raise
        StagedSwitchNotReady if the group can no longer be taken down
        safely: the staged files are left behind (they are inert) and the
        next pass starts over from groups()."""

    def before_switch(self, group: StagedGroup) -> None:
        """Last word before the first daemon is restarted: the group is
        down (take_down ran, possibly a while ago on a resumed upgrade) and
        nothing has been switched yet. Raise StagedSwitchNotReady to call it
        off: the runner restores the group and starts over next pass."""
        return None

    def after_switch(self, group: StagedGroup) -> None:
        """Called once the group is verified and restored on the target
        release (not after a rollback)."""
        return None

    def forget(self, state: Dict[str, Any], names: List[str]) -> None:
        """Daemons of a persisted group that are no longer known to cephadm
        (purged by the admin) are dropped from it on resume; adjust the
        policy's own `data` and `snapshot` entries in `state` accordingly."""
        return None

    def is_down(self, group: StagedGroup) -> bool:
        """Whether the group is currently out of service (used on resume)."""
        return True

    @abstractmethod
    def snapshot(self, group: StagedGroup) -> Dict[str, Any]:
        """What verify() compares against, taken right before the switch."""

    @abstractmethod
    def verify(self, group: StagedGroup, snapshot: Dict[str, Any],
               target_version: Optional[str]) -> Tuple[bool, str]:
        """Ask the monitors whether every daemon of the group has restarted
        since `snapshot` and - if target_version is given - runs it.
        Returns (ok, reason)."""

    @abstractmethod
    def restore(self, group: StagedGroup) -> None:
        """Put the group back into service. Called after a successful
        verify, and after a rollback."""


class StagedSwitchRunner:
    """Drives one group through stage / down / switch / verify / restore
    within one serve() pass. The caller returns afterwards; the next pass
    re-evaluates what is left."""

    def __init__(self, upgrade: 'CephadmUpgrade', policy: StagedSwitchPolicy) -> None:
        self.upgrade = upgrade
        self.mgr = upgrade.mgr
        self.policy = policy

    # ---------------------------------------------------------------- state
    @property
    def state(self) -> Dict[str, Any]:
        assert self.upgrade.upgrade_state is not None
        return self.upgrade.upgrade_state.staged_switch

    def _set_phase(self, phase: str) -> None:
        self.state['phase'] = phase
        self.upgrade._save_upgrade_state()

    def _clear(self) -> None:
        assert self.upgrade.upgrade_state is not None
        self.upgrade.upgrade_state.staged_switch = {}
        self.upgrade._save_upgrade_state()

    def _fail(self, alert_id: str, summary: str, detail: List[str]) -> None:
        self.upgrade._fail_upgrade(alert_id, {
            'severity': 'warning',
            'summary': summary,
            'count': max(1, len(detail)),
            'detail': detail,
        })

    # ------------------------------------------------------------ cephadm ops
    @staticmethod
    def _by_host(group: StagedGroup) -> Dict[str, List[DaemonDescription]]:
        by_host: Dict[str, List[DaemonDescription]] = {}
        for d in group.daemons:
            assert d.hostname is not None
            by_host.setdefault(d.hostname, []).append(d)
        return by_host

    async def _stage_all(self, group: StagedGroup, target_image: str) -> Dict[str, str]:
        """deploy --stage every daemon: up to max_parallel hosts at a time,
        the daemons of one host one after the other (each staging starts
        containers of its own; cephadm's per-host lock would serialize them
        anyway). name -> error."""
        sem = asyncio.Semaphore(max(1, int(self.mgr.upgrade_staged_switch_max_parallel)))
        errors: Dict[str, str] = {}
        # one osdmap read for the whole group rather than one per OSD
        osd_uuid_map: Optional[Dict[str, Any]] = None
        if any(d.daemon_type == 'osd' for d in group.daemons):
            try:
                osd_uuid_map = self.mgr.get_osd_uuid_map()
            except Exception as e:
                logger.debug('Upgrade: could not read the osd uuid map up front: %s', e)

        async def one(d: DaemonDescription) -> None:
            assert d.daemon_type is not None and d.daemon_id is not None
            try:
                self.mgr._daemon_action_set_image('redeploy', target_image, d.daemon_type, d.daemon_id)
                spec = CephadmDaemonDeploySpec.from_daemon_description(d)
                if d.daemon_type != 'osd':
                    spec = self.mgr.cephadm_services[
                        daemon_type_to_service(d.daemon_type)].prepare_create(spec)
                else:
                    # like _daemon_action: OSDs get their config refreshed
                    # but not the full prepare_create
                    spec.final_config, spec.deps = self.mgr.osd_service.generate_config(spec)
                await CephadmServe(self.mgr)._create_daemon(
                    spec, osd_uuid_map=osd_uuid_map, stage=True)
            except Exception as e:
                errors[d.name()] = str(e)

        async def host(daemons: List[DaemonDescription]) -> None:
            async with sem:
                for d in daemons:
                    await one(d)

        await asyncio.gather(*[host(ds) for ds in self._by_host(group).values()])
        return errors

    async def _switch_all(self, group: StagedGroup, target_image: str,
                          rollback: bool = False) -> Dict[str, str]:
        """cephadm switch-staged, one call per host naming every daemon of
        the group on it (one stop / one start for all of them), up to
        max_parallel hosts at a time. name -> error."""
        sem = asyncio.Semaphore(max(1, int(self.mgr.upgrade_staged_switch_max_parallel)))
        errors: Dict[str, str] = {}

        async def host(hostname: str, daemons: List[DaemonDescription]) -> None:
            args: List[str] = []
            for d in daemons:
                args += ['--name', d.name()]
            args += ['--rollback'] if rollback else ['--expected-image', target_image]
            async with sem:
                try:
                    out, err, code = await CephadmServe(self.mgr)._run_cephadm(
                        hostname, daemons[0].name(), 'switch-staged', args,
                        image=target_image, error_ok=True)
                    if code:
                        # the command checks every daemon before stopping
                        # any; a failure is the host's, attribute it to each
                        why = '\n'.join(err) or f'exit code {code}'
                        for d in daemons:
                            errors[d.name()] = why
                except Exception as e:
                    for d in daemons:
                        errors[d.name()] = str(e)

        await asyncio.gather(*[host(h, ds) for h, ds in self._by_host(group).items()])
        return errors

    async def _refresh_hosts(self, hosts: List[str]) -> None:
        """cephadm's daemon cache still shows the previous image for the
        hosts of the group; refresh it now so the next pass moves on."""
        async def one(host: str) -> None:
            try:
                ls = await CephadmServe(self.mgr)._run_cephadm_json(
                    host, 'mon', 'ls', [], no_fsid=True,
                    log_output=self.mgr.log_refresh_metadata)
                self.mgr._process_ls_output(host, ls)
            except Exception as e:
                logger.warning('Upgrade: refreshing daemons of %s failed: %s', host, e)
                self.mgr.cache.invalidate_host_daemons(host)
        await asyncio.gather(*[one(h) for h in hosts])

    def _drop_image_pins(self, group: StagedGroup) -> None:
        """Forget the per-daemon container_image set for staging, so the
        reconciler does not redeploy the new image on its own later."""
        for d in group.daemons:
            try:
                self.mgr.check_mon_command({
                    'prefix': 'config rm', 'name': 'container_image',
                    'who': name_to_config_section(d.name())})
            except Exception as e:
                logger.warning('Upgrade: could not drop container_image for %s: %s', d.name(), e)

    def _wait(self, group: StagedGroup, snapshot: Dict[str, Any],
              target_version: Optional[str], timeout: int) -> Tuple[bool, str]:
        deadline = time.time() + timeout
        while True:
            ok, why = self.policy.verify(group, snapshot, target_version)
            if ok:
                return True, ''
            if time.time() >= deadline:
                return False, why
            time.sleep(2)

    # --------------------------------------------------------------- driver
    def _group_from_state(self, need_upgrade: List[DaemonDescription]) -> Optional[StagedGroup]:
        st = self.state
        if not st or st.get('type') != self.policy.daemon_type:
            return None
        by_name = {d.name(): d for d in self.mgr.cache.get_daemons_by_type(self.policy.daemon_type)}
        missing = [n for n in st['daemons'] if n not in by_name]
        if missing:
            # Removed from cephadm since the group was formed - typically
            # the admin purged a daemon that did not come back after the
            # switch. Go on without it rather than wedge the upgrade.
            logger.warning('Upgrade: %s no longer known to cephadm; resuming the staged '
                           'switch of %s without %s', ', '.join(missing), st['label'],
                           'it' if len(missing) == 1 else 'them')
            st['daemons'] = [n for n in st['daemons'] if n in by_name]
            self.policy.forget(st, missing)
            self.upgrade._save_upgrade_state()
            if not st['daemons']:
                self._clear()
                return None
        group = StagedGroup(st['key'], st['label'], [by_name[n] for n in st['daemons']], st.get('data'))
        group.snapshot = st.get('snapshot') or {}
        return group

    def _new_group(self, group: StagedGroup, target_image: str) -> Optional[StagedGroup]:
        offline = sorted(h for h in group.hosts if h in self.mgr.offline_hosts)
        reason = f'host(s) {", ".join(offline)} offline' if offline else self.policy.preconditions(group)
        if reason:
            self._fail('UPGRADE_STAGE_FAILED',
                       f'Cannot stage the upgrade of {group.label}: {reason}', [reason])
            return None
        assert self.upgrade.upgrade_state is not None
        self.upgrade.upgrade_state.staged_switch = {
            'type': self.policy.daemon_type, 'key': group.key, 'label': group.label,
            'daemons': group.names, 'hosts': group.hosts, 'image': target_image,
            'data': group.data, 'snapshot': {}, 'phase': PHASE_STAGING,
        }
        self.upgrade._save_upgrade_state()
        return group

    def _not_ready(self, what: str, reason: str) -> None:
        msg = f'Waiting to stage {what}: {reason}'
        logger.info('Upgrade: %s', msg)
        self.upgrade.upgrade_info_str = msg

    def _count_against_limit(self, group: StagedGroup) -> None:
        """`ceph orch upgrade start --limit N`: the daemons of a switched
        group count like redeployed ones (redeploy-only ones excluded)."""
        state = self.upgrade.upgrade_state
        assert state is not None
        if state.remaining_count is None:
            return
        redeploy_only = set(self.state.get('redeploy_only') or [])
        state.remaining_count -= len([n for n in group.names if n not in redeploy_only])
        # saved by the caller's _clear(), in the same write as the end of
        # the group, so a failover in between cannot count it twice

    def run(self, need_upgrade: List[DaemonDescription], target_image: str,
            redeploy_only: Optional[Iterable[str]] = None) -> bool:
        """Handle one group. Returns False when the policy found nothing
        to handle (the caller then upgrades these daemons the regular
        way); True when the group was handled, the policy asked to wait,
        or the upgrade was paused.

        `redeploy_only` names the daemons of need_upgrade that are already
        on the target image and only need a redeploy (they do not count
        against `--limit`)."""
        assert self.upgrade.upgrade_state is not None
        target_version = self.upgrade.upgrade_state.target_version
        timeout = self.policy.verify_timeout()
        remaining = self.upgrade.upgrade_state.remaining_count

        group = self._group_from_state(need_upgrade)
        if group is None:
            if remaining is not None and remaining <= 0:
                return False  # --limit reached; the regular path ends the upgrade
            try:
                groups = self.policy.groups(need_upgrade)
            except StagedSwitchNotReady as e:
                self._not_ready(f'{self.policy.daemon_type} daemons', str(e))
                return True
            except OrchestratorError as e:
                self._fail('UPGRADE_STAGE_FAILED',
                           f'Cannot stage the upgrade of {self.policy.daemon_type} daemons: {e}', [str(e)])
                return True
            if not groups:
                return False
            group = self._new_group(groups[0], target_image)
            if group is None:
                return True
            self.state['redeploy_only'] = sorted(
                n for n in (redeploy_only or []) if n in set(group.names))
            self.upgrade._save_upgrade_state()
        else:
            target_image = self.state.get('image') or target_image
        st = self.state
        phase = st.get('phase')
        logger.info('Upgrade: staged switch of %s (%d %s on %d host(s)), phase %s',
                    group.label, len(group.daemons), self.policy.daemon_type, len(group.hosts), phase)

        if phase == PHASE_STAGING:
            self.upgrade.upgrade_info_str = f'Staging {self.policy.daemon_type} of {group.label}'
            errors = self.mgr.wait_async(self._stage_all(group, target_image))
            if errors:
                # Nothing has been taken down; staged files are inert.
                self._clear()
                self._fail('UPGRADE_STAGE_FAILED',
                           f'Staging the upgrade of {group.label} failed on '
                           f'{len(errors)} daemon(s); nothing was restarted',
                           [f'{k}: {v}' for k, v in errors.items()])
                return True
            self._set_phase(PHASE_STAGED)
            phase = PHASE_STAGED

        if phase == PHASE_STAGED:
            self.upgrade.upgrade_info_str = f'Taking {group.label} down for the staged switch'
            try:
                self.policy.take_down(group)
                group.snapshot = self.policy.snapshot(group)
                st['snapshot'] = group.snapshot
                st['data'] = group.data
                self._set_phase(PHASE_DOWN)
            except StagedSwitchNotReady as e:
                # The cluster changed under us since the group was chosen
                # (OSD: a bucket that was ok-to-stop no longer is). Nothing
                # was restarted; the staged files are inert and get
                # overwritten by the next staging. Start over next pass so
                # the policy can pick another group.
                self._drop_image_pins(group)
                self._clear()
                self._not_ready(group.label, f'{e}; a group will be chosen again next pass')
                return True
            except Exception as e:
                # Nothing has been switched: restore whatever was taken
                # down, keep the staged files for a retry, pause.
                logger.error('Upgrade: could not take %s down for the staged switch: %s', group.label, e)
                try:
                    self.policy.restore(group)
                finally:
                    self._clear()
                self._fail('UPGRADE_STAGE_FAILED',
                           f'Could not take {group.label} down for the staged switch: {e}; '
                           f'nothing was restarted', [str(e)])
                return True
            phase = PHASE_DOWN

        if phase in (PHASE_DOWN, PHASE_SWITCHING):
            if phase == PHASE_SWITCHING or not self.policy.is_down(group):
                # Resuming after a mgr failover: make sure the group really
                # is out of service before any daemon is restarted.
                self.policy.take_down(group)
            if phase == PHASE_DOWN:
                try:
                    self.policy.before_switch(group)
                except StagedSwitchNotReady as e:
                    # Nothing switched yet (OSD: the group is no longer
                    # ok-to-stop, e.g. a failover brought us here long after
                    # take_down). Put it back, start over next pass.
                    self.policy.restore(group)
                    self._drop_image_pins(group)
                    self._clear()
                    self._not_ready(group.label, f'{e}; a group will be chosen again next pass')
                    return True
            self.upgrade.upgrade_info_str = f'Switching {self.policy.daemon_type} of {group.label} to {target_image}'
            self._set_phase(PHASE_SWITCHING)
            errors = self.mgr.wait_async(self._switch_all(group, target_image))
            if errors:
                why = 'switch-staged failed on ' + ', '.join(f'{k} ({v})' for k, v in errors.items())
                if self.policy.rollback_on_failure:
                    self._rollback(group, target_image, why, unswitched=set(errors))
                else:
                    self._pause_keeping_state(group, why, [f'{k}: {v}' for k, v in errors.items()])
                return True
            self._set_phase(PHASE_SWITCHED)
            phase = PHASE_SWITCHED

        if phase == PHASE_SWITCHED:
            self.upgrade.upgrade_info_str = f'Waiting for the {self.policy.daemon_type} of {group.label} on {target_version}'
            ok, why = self._wait(group, group.snapshot, target_version, timeout)
            if not ok:
                if self.policy.rollback_on_failure:
                    self._rollback(group, target_image, why)
                else:
                    self._pause_keeping_state(group, why, [why])
                return True
            logger.info('Upgrade: all %d %s of %s are back on %s; restoring',
                        len(group.daemons), self.policy.daemon_type, group.label, target_version)
            self.policy.restore(group)
            self.policy.after_switch(group)
            self._count_against_limit(group)
            self.mgr.wait_async(self._refresh_hosts(group.hosts))
            self._clear()
            logger.info('Upgrade: %s back on %s', group.label, target_version)
            return True

        if phase == PHASE_ROLLING_BACK:
            self._rollback(group, target_image, 'resumed after a mgr failover during rollback')
        return True

    def _pause_keeping_state(self, group: StagedGroup, reason: str, detail: List[str]) -> None:
        """A switch that did not complete, for a policy without rollback:
        pause the upgrade and keep the group's state at its current phase,
        so `ceph orch upgrade resume` retries the switch (idempotent) or
        the verification right there, once the daemons listed are dealt
        with. The group stays out of service meanwhile (OSD: noout set)."""
        logger.error('Upgrade: the staged switch of %s did not complete: %s; pausing, '
                     'the upgrade resumes at this group', group.label, reason)
        self._fail('UPGRADE_SWITCH_FAILED',
                   f'Switching {group.label} to the staged image did not complete ({reason}). '
                   f'The daemons are left as they are, on the new image where the switch went '
                   f'through; fix the ones listed, then `ceph orch upgrade resume` retries from '
                   f'this group. `cephadm switch-staged --rollback` puts a daemon back by hand',
                   detail or [reason])

    def _rollback(self, group: StagedGroup, target_image: str, reason: str,
                  unswitched: Optional[Set[str]] = None) -> None:
        """Undo a switch that did not complete: previous unit files back,
        daemons back on the previous release, group restored, upgrade paused.

        `unswitched`: daemons whose switch-staged call failed. The command
        checks everything before stopping anything, so these never
        restarted: their rollback only makes sure they run, and they are
        not expected to re-register."""
        logger.error('Upgrade: rolling back the staged switch of %s: %s', group.label, reason)
        self._set_phase(PHASE_ROLLING_BACK)
        restarted = StagedGroup(group.key, group.label,
                                [d for d in group.daemons if d.name() not in (unswitched or set())],
                                group.data)
        before = self.policy.snapshot(restarted)
        errors = self.mgr.wait_async(self._switch_all(group, target_image, rollback=True))
        detail = [f'{k}: {v}' for k, v in errors.items()]
        if not errors and restarted.daemons:
            ok, why = self._wait(restarted, before, None, self.policy.verify_timeout())
            if not ok:
                detail.append(f'after rollback: {why}')
        self._drop_image_pins(group)
        if not detail:
            self.policy.restore(group)
            summary = (f'Switching {group.label} to the staged image failed ({reason}); '
                       f'rolled back and restored on the previous image')
        else:
            summary = (f'Switching {group.label} to the staged image failed ({reason}) '
                       f'and the rollback did not complete; {group.label} is left out of service. '
                       f'Fix the daemons listed, then restore it by hand')
        self._clear()
        self._fail('UPGRADE_SWITCH_FAILED', summary, detail or [reason])


# ======================================================================
# OSD: every OSD still to upgrade under one CRUSH bucket, when the
# monitors say the whole set can be stopped with every PG staying active
# ======================================================================

OSD_CRUSH_LEVEL_AUTO = 'auto'
# like CephadmUpgrade._wait_for_ok_to_stop: a few tries within one pass,
# then let the next pass ask again
OSD_OK_TO_STOP_TRIES = 4
OSD_OK_TO_STOP_RETRY_SECONDS = 15


def _natural_key(name: str) -> List[Any]:
    """host2 before host10."""
    return [(0, int(p), '') if p.isdigit() else (1, 0, p) for p in re.split(r'(\d+)', name)]


class CrushTree:
    """Read-only view of the mgr's `osd_map_tree`: buckets, their types, and
    the OSDs under each of them. Device-class shadow buckets are not part of
    that dump, so a bucket is seen once whatever classes it mixes."""

    def __init__(self, tree: Dict[str, Any]) -> None:
        self.nodes: Dict[int, Dict[str, Any]] = {}
        self.by_name: Dict[str, Dict[str, Any]] = {}
        self.osd_ids: Set[int] = set()
        has_parent: Set[int] = set()
        for n in tree.get('nodes', []) or []:
            nid = int(n['id'])
            if nid >= 0:
                self.osd_ids.add(nid)
                continue
            self.nodes[nid] = n
            self.by_name[str(n.get('name'))] = n
            for c in n.get('children', []) or []:
                has_parent.add(int(c))
        self.roots: List[int] = [i for i in self.nodes if i not in has_parent]

    def osds_under(self, bucket_id: int) -> List[int]:
        out: List[int] = []
        seen: Set[int] = set()
        stack = [bucket_id]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if cur >= 0:
                out.append(cur)
                continue
            node = self.nodes.get(cur)
            if node:
                stack.extend(int(c) for c in (node.get('children') or []))
        return sorted(out)

    def buckets_of_type(self, btype: str) -> List[Dict[str, Any]]:
        return sorted((n for n in self.nodes.values() if n.get('type') == btype),
                      key=lambda n: _natural_key(str(n.get('name'))))

    def bucket_types_top_down(self) -> List[str]:
        """Bucket types present below a root, highest first (by type id):
        the levels `auto` tries, in order. Roots themselves are left out -
        stopping a whole hierarchy is never what an upgrade wants."""
        by_type: Dict[str, int] = {}
        for i, n in self.nodes.items():
            if i in self.roots:
                continue
            by_type[str(n.get('type'))] = max(by_type.get(str(n.get('type')), -1), int(n.get('type_id', 0)))
        return [t for t, _ in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))]


class OsdStagedSwitchPolicy(StagedSwitchPolicy):
    """One CRUSH bucket per group: all the OSDs under it that still need the
    upgrade, restarted together, provided `osd ok-to-stop` on that exact set
    says every PG stays active (>= min_size) without them. The bucket type
    is `upgrade_staged_switch_osd_crush_level` (default `host`), or with
    `auto` the highest type below the root for which such a bucket exists
    right now, re-evaluated for every group: a rack whose hosts hold too many
    replicas of a pool is upgraded host by host while the others go in one
    go, and nothing is ever restarted that the monitors did not clear."""

    daemon_type = 'osd'
    # An OSD that booted on the new release may have upgraded its store
    # (BlueStore/RocksDB formats, omap layouts); starting the previous
    # ceph-osd on it is a downgrade Ceph does not support. Never roll a
    # group back: pause and resume from the same phase instead.
    rollback_on_failure = False

    def __init__(self, upgrade: 'CephadmUpgrade') -> None:
        super().__init__(upgrade)
        # (osd id, up_from) -> ceph_version_short, so `osd metadata` is asked
        # once per OSD restart, not once per verify poll
        self._versions: Dict[Tuple[int, int], str] = {}

    # -------------------------------------------------------------- options
    def _level(self) -> str:
        return str(getattr(self.mgr, 'upgrade_staged_switch_osd_crush_level', 'host') or 'host').strip().lower()

    def _noout(self) -> bool:
        return bool(getattr(self.mgr, 'upgrade_staged_switch_osd_noout', True))

    def _max_group(self) -> int:
        return max(0, int(getattr(self.mgr, 'upgrade_staged_switch_osd_max_group', 0) or 0))

    def verify_timeout(self) -> int:
        # a host of OSDs booting at once takes longer than an MDS group:
        # store open, PG load, on-disk conversions on a major upgrade
        return int(getattr(self.mgr, 'upgrade_staged_switch_osd_timeout', 600) or 600)

    # -------------------------------------------------------------- helpers
    def _tree(self) -> CrushTree:
        return CrushTree(self.mgr.get('osd_map_tree') or {})

    def _osdmap(self) -> Dict[str, Any]:
        return self.mgr.get('osd_map') or {}

    def _osds(self, osdmap: Optional[Dict[str, Any]] = None) -> Dict[int, Dict[str, Any]]:
        return {int(o['osd']): o for o in (osdmap or self._osdmap()).get('osds', []) or []}

    @staticmethod
    def _names(ids: Iterable[int]) -> List[str]:
        return [f'osd.{i}' for i in ids]

    @staticmethod
    def _up_fingerprint(osds: Dict[int, Dict[str, Any]], excluding: Iterable[int]) -> str:
        """Which OSDs outside the group the monitors see up, as a digest:
        compared between the choice of the group and the switch, it tells
        whether an OSD the ok-to-stop verdict counted on has gone down
        (or come back) meanwhile, whatever the PG stats say yet."""
        skip = set(excluding)
        up = sorted(i for i, o in osds.items() if o.get('up') and i not in skip)
        return hashlib.sha1(','.join(str(i) for i in up).encode()).hexdigest()

    def _ok_to_stop(self, ids: List[int]) -> Tuple[bool, str]:
        """Ask the monitors whether *exactly* this set can be stopped with
        every PG staying active. `max` = len(ids) keeps the mgr from adding
        OSDs of its own to the set."""
        ret, out, err = self.mgr.mon_command({
            'prefix': 'osd ok-to-stop', 'ids': [str(i) for i in ids], 'max': len(ids)})
        if ret == 0:
            return True, ''
        why = (err or '').strip() or f'osd ok-to-stop returned {ret}'
        try:
            report = json.loads(out or '{}')
            report = report.get('ok_to_stop', report) if isinstance(report, dict) else {}
            inactive = report.get('bad_become_inactive') or []
            already = report.get('bad_already_inactive') or []
            unknown = report.get('unknown_pgs') or []
            no_pool = report.get('bad_no_pool_pgs') or []
            bits = []
            if inactive:
                bits.append(f'{len(inactive)} PG(s) would become inactive')
            if already:
                bits.append(f'{len(already)} PG(s) already inactive')
            if unknown:
                bits.append(f'{len(unknown)} PG(s) unknown')
            if no_pool:
                bits.append(f'{len(no_pool)} PG(s) of a pool being created or deleted')
            if bits:
                why = ', '.join(bits)
        except (ValueError, TypeError, AttributeError):
            pass
        return False, why

    def _paused(self) -> bool:
        return bool(self.upgrade.upgrade_state is None or self.upgrade.upgrade_state.paused)

    def _version(self, osd_id: int, up_from: int) -> str:
        """ceph_version_short of osd_id as the monitors recorded it at its
        last boot (`osd metadata`), not cephadm's cache nor the mgr's
        daemon state, which can miss a boot epoch."""
        key = (osd_id, up_from)
        if key in self._versions:
            return self._versions[key]
        ret, out, err = self.mgr.mon_command({'prefix': 'osd metadata', 'id': osd_id, 'format': 'json'})
        if ret != 0:
            return ''
        try:
            md = json.loads(out or '{}')
        except ValueError:
            return ''
        v = str(md.get('ceph_version_short') or '')
        if not v and str(md.get('ceph_version', '')).startswith('ceph version '):
            v = str(md['ceph_version']).split(' ')[2]
        if v:
            self._versions[key] = v
        return v

    # --------------------------------------------------------------- policy
    def _levels(self, tree: CrushTree) -> List[str]:
        level = self._level()
        if level == OSD_CRUSH_LEVEL_AUTO:
            levels = tree.bucket_types_top_down()
            if not levels:
                raise OrchestratorError('the CRUSH map has no bucket below a root')
            return levels
        if level == 'osd':
            raise OrchestratorError(
                "mgr/cephadm/upgrade_staged_switch_osd_crush_level 'osd' makes no sense "
                "for a staged switch (one OSD at a time is the regular upgrade path)")
        present = {str(n.get('type')) for n in tree.nodes.values()}
        if level not in present:
            # a map without that level (hosts straight under the root with
            # level 'rack', say): nothing to group by, let the regular path
            # do its job rather than stall the upgrade
            logger.warning('Upgrade: mgr/cephadm/upgrade_staged_switch_osd_crush_level %r is not a '
                           'bucket type of the CRUSH map (found: %s); OSDs are upgraded the '
                           'regular way', level, ', '.join(sorted(present)) or 'none')
            return []
        if all(int(n['id']) in tree.roots for n in tree.nodes.values() if n.get('type') == level):
            raise OrchestratorError(
                f'mgr/cephadm/upgrade_staged_switch_osd_crush_level {level!r} is only used by '
                f'root buckets; a whole hierarchy cannot be switched at once')
        return [level]

    def _pending(self, need_upgrade: List[DaemonDescription], tree: CrushTree,
                 osds: Dict[int, Dict[str, Any]]) -> Dict[int, DaemonDescription]:
        """OSDs of need_upgrade this policy will handle: in the upgrade's
        CRUSH scope, on an online host, up, and placed in the CRUSH map. The
        others are left to the regular path, which has its own handling."""
        state = self.upgrade.upgrade_state
        assert state is not None
        pending: Dict[int, DaemonDescription] = {}
        for d in need_upgrade:
            if d.daemon_type == 'osd' and str(d.daemon_id).isdigit():
                pending[int(str(d.daemon_id))] = d
        if not pending:
            return {}
        scope_name = getattr(state, 'crush_bucket_name', None)  # not in reef's UpgradeState
        if scope_name:
            node = tree.by_name.get(scope_name)
            if node is None:
                raise OrchestratorError(
                    f'CRUSH bucket {scope_name!r} (--crush_bucket_name) not found')
            scope = set(tree.osds_under(int(node['id'])))
            pending = {i: d for i, d in pending.items() if i in scope}
        skipped: List[str] = []
        for i in sorted(pending):
            why = None
            if pending[i].hostname in self.mgr.offline_hosts:
                why = 'host offline'
            elif not osds.get(i, {}).get('up'):
                why = 'not up'
            elif i not in tree.osd_ids:
                why = 'not in the CRUSH map'
            if why:
                skipped.append(f'osd.{i} ({why})')
                del pending[i]
        if skipped:
            logger.info('Upgrade: staged switch leaves %s to the regular upgrade path',
                        ', '.join(skipped))
        return pending

    def _pick(self, tree: CrushTree, levels: List[str], pending: Dict[int, DaemonDescription],
              limit: Optional[int]) -> Tuple[Optional[StagedGroup], List[str], bool]:
        """First bucket, highest level first, whose pending OSDs are
        ok-to-stop as a set. Returns (group, reasons it skipped the others,
        whether any bucket of these levels holds a pending OSD at all)."""
        reasons: List[str] = []
        any_bucket = False
        for btype in levels:
            for bucket in tree.buckets_of_type(btype):
                ids = [i for i in tree.osds_under(int(bucket['id'])) if i in pending]
                if not ids:
                    continue
                any_bucket = True
                if limit is not None and len(ids) > limit:
                    ids = ids[:limit]
                if self._max_group() and len(ids) > self._max_group():
                    reasons.append(f'{btype} {bucket["name"]}: {len(ids)} OSDs, more than '
                                   f'upgrade_staged_switch_osd_max_group ({self._max_group()})')
                    continue
                ok, why = self._ok_to_stop(ids)
                if ok:
                    label = f'{btype} {bucket["name"]}'
                    group = StagedGroup(str(bucket['id']), label, [pending[i] for i in ids], {
                        'bucket': bucket['name'], 'type': btype, 'osd_ids': ids,
                        'noout': False, 'committed': False,
                        'up_fingerprint': self._up_fingerprint(self._osds(), ids)})
                    return group, reasons, True
                reasons.append(f'{btype} {bucket["name"]} ({len(ids)} OSDs): {why}')
            if reasons and self._level() == OSD_CRUSH_LEVEL_AUTO:
                logger.info('Upgrade: no %s can be switched as a whole right now (%s); '
                            'trying the next CRUSH level down', btype, '; '.join(reasons[-3:]))
        return None, reasons, any_bucket

    def groups(self, need_upgrade: List[DaemonDescription]) -> List[StagedGroup]:
        state = self.upgrade.upgrade_state
        assert state is not None
        tree = self._tree()
        pending = self._pending(need_upgrade, tree, self._osds())
        if not pending:
            return []
        levels = self._levels(tree)
        if not levels:
            return []
        limit = state.remaining_count if state.remaining_count is not None and state.remaining_count > 0 else None
        reasons: List[str] = []
        for attempt in range(OSD_OK_TO_STOP_TRIES):
            if attempt:
                if self._paused():
                    raise StagedSwitchNotReady('upgrade paused')
                time.sleep(OSD_OK_TO_STOP_RETRY_SECONDS)
            group, reasons, any_bucket = self._pick(tree, levels, pending, limit)
            if group is not None:
                logger.info('Upgrade: staged switch picked %s: %d OSD(s) %s', group.label,
                            len(group.daemons), ', '.join(group.names))
                return [group]
            if not any_bucket:
                # e.g. level 'rack' on a map whose hosts hang off the root
                logger.info('Upgrade: no %s bucket holds an OSD still to upgrade; '
                            'the regular upgrade path takes over', '/'.join(levels))
                return []
        summary = (f'no {"/".join(levels)} bucket can be switched as a whole right now, every PG '
                   f'must stay active ({"; ".join(reasons[:3])}{"; ..." if len(reasons) > 3 else ""})')
        if self._some_osd_ok_to_stop_alone(tree, levels, pending):
            # Not the PGs of the previous group recovering (then no OSD
            # sharing a PG with them passes either) but buckets that cannot
            # go as a whole: a pool with an `osd` failure domain, two copies
            # of a PG on one host... Rather than wait for a verdict that
            # will not change, let the regular path upgrade OSDs one by one
            # (`osd ok-to-stop` batches) this pass; the buckets are tried
            # again next pass with fewer OSDs left in them.
            logger.info('Upgrade: %s; single OSDs are, so the regular upgrade path '
                        'handles this pass', summary)
            return []
        raise StagedSwitchNotReady(summary)

    def _some_osd_ok_to_stop_alone(self, tree: CrushTree, levels: List[str],
                                   pending: Dict[int, DaemonDescription], probes: int = 8) -> bool:
        """Whether one OSD, alone, of some bucket that was refused as a whole
        is ok-to-stop. A few probes at most, lowest level, so a large map
        does not turn into hundreds of PG scans."""
        tried = 0
        for bucket in tree.buckets_of_type(levels[-1]):
            ids = [i for i in tree.osds_under(int(bucket['id'])) if i in pending]
            if not ids:
                continue
            ok, _ = self._ok_to_stop(ids[:1])
            if ok:
                return True
            tried += 1
            if tried >= probes:
                break
        return False

    def take_down(self, group: StagedGroup) -> None:
        ids = [int(i) for i in group.data['osd_ids']]
        if self._noout():
            # Keep the monitors from marking the group out should the
            # restart outlast mon_osd_down_out_interval. Idempotent.
            self.mgr.check_mon_command({
                'prefix': 'osd set-group', 'flags': 'noout', 'who': self._names(ids)})
            group.data['noout'] = True
        group.data['committed'] = True

    def before_switch(self, group: StagedGroup) -> None:
        # Staging took a while - or a mgr failover brought us back here long
        # after take_down: make sure the window can still be opened. Not
        # called once a switch has started (PGs are degraded by then).
        ids = [int(i) for i in group.data['osd_ids']]
        # Any OSD outside the group down (or back) since the group was
        # chosen means the ok-to-stop verdict was given for another
        # cluster: start over, whatever a fresh verdict would say. ok-to-stop
        # works from PG stats, which trail an OSD failure by the heartbeat
        # grace and a stats report; the osdmap's up set is the earliest
        # the monitors can tell us about one.
        fp = group.data.get('up_fingerprint')
        if fp and self._up_fingerprint(self._osds(), ids) != fp:
            raise StagedSwitchNotReady(
                f'the set of up OSDs changed since {group.label} was chosen')
        for attempt in range(OSD_OK_TO_STOP_TRIES):
            ok, why = self._ok_to_stop(ids)
            if ok:
                return
            if attempt == OSD_OK_TO_STOP_TRIES - 1 or self._paused():
                raise StagedSwitchNotReady(f'{group.label} is no longer ok-to-stop: {why}')
            time.sleep(OSD_OK_TO_STOP_RETRY_SECONDS)

    def forget(self, state: Dict[str, Any], names: List[str]) -> None:
        gone = {int(n.split('.', 1)[1]) for n in names if n.split('.', 1)[1].isdigit()}
        data = state.get('data') or {}
        data['osd_ids'] = [i for i in data.get('osd_ids', []) if int(i) not in gone]
        snap = state.get('snapshot') or {}
        if 'up_from' in snap:
            snap['up_from'] = {k: v for k, v in snap['up_from'].items() if int(k) not in gone}

    def is_down(self, group: StagedGroup) -> bool:
        if not group.data.get('committed'):
            return False
        if not group.data.get('noout'):
            return True
        osds = self._osds()
        return all('noout' in (osds.get(int(i), {}).get('state') or []) for i in group.data['osd_ids'])

    def snapshot(self, group: StagedGroup) -> Dict[str, Any]:
        osdmap = self._osdmap()
        osds = self._osds(osdmap)
        return {'epoch': int(osdmap.get('epoch', 0)),
                'up_from': {str(i): int(osds.get(int(i), {}).get('up_from', 0)) for i in group.data['osd_ids']}}

    def verify(self, group: StagedGroup, snapshot: Dict[str, Any],
               target_version: Optional[str]) -> Tuple[bool, str]:
        ids = [int(i) for i in group.data['osd_ids']]
        pre = {int(k): int(v) for k, v in (snapshot.get('up_from') or {}).items()}
        osds = self._osds()
        # the osdmap first (cheap, and `osd metadata` only means something
        # once the OSD has booted again)
        for i in ids:
            o = osds.get(i)
            if not o or not o.get('up'):
                return False, f'osd.{i} is not up yet'
            if int(o.get('up_from', 0)) <= pre.get(i, 0):
                return False, f'osd.{i} has not re-registered with the monitors yet (up_from {o.get("up_from")})'
        if target_version:
            for i in ids:
                v = self._version(i, int(osds[i].get('up_from', 0)))
                if v != target_version:
                    return False, f'osd.{i} reports version {v!r}, want {target_version!r}'
        return True, ''

    def restore(self, group: StagedGroup) -> None:
        if group.data.get('noout'):
            ids = [int(i) for i in group.data['osd_ids']]
            try:
                self.mgr.check_mon_command({
                    'prefix': 'osd unset-group', 'flags': 'noout', 'who': self._names(ids)})
                group.data['noout'] = False
            except Exception as e:
                # The group is switched either way; a leftover per-OSD noout
                # shows up as OSD_FLAGS in `ceph health detail`.
                logger.warning('Upgrade: could not unset noout on %s (%s); run '
                               '`ceph osd unset-group noout %s` to clear it', group.label, e,
                               ' '.join(self._names(ids)))


# Reef: only the OSD policy lives here. The staged MDS switch of this
# backport predates the runner and is implemented in
# CephadmUpgrade._staged_mds_upgrade (mgr/cephadm/upgrade_mds_staged).
POLICIES: Dict[str, Type[StagedSwitchPolicy]] = {
    OsdStagedSwitchPolicy.daemon_type: OsdStagedSwitchPolicy,
}


def policy_for(upgrade: 'CephadmUpgrade', daemon_type: str) -> Optional[StagedSwitchPolicy]:
    """The policy to use for daemon_type in this upgrade, or None when the
    staged switch is off, not configured for the type, or not usable now."""
    mgr = upgrade.mgr
    if not getattr(mgr, 'upgrade_staged_switch', False):
        return None
    types = [t.strip() for t in str(getattr(mgr, 'upgrade_staged_switch_types', '') or '').split(',')]
    if daemon_type not in types:
        return None
    cls = POLICIES.get(daemon_type)
    if cls is None:
        logger.warning('Upgrade: upgrade_staged_switch_types lists %s but there is '
                       'no staged switch policy for it; upgrading it the regular way', daemon_type)
        return None
    policy = cls(upgrade)
    if not policy.enabled():
        return None
    return policy
