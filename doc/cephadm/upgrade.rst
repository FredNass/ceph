==============
Upgrading Ceph
==============

.. _cephadm-upgrade:

Cephadm can safely upgrade Ceph from one point release to the next.  For
example, you can upgrade from v15.2.0 (the first Octopus release) to the next
point release, v15.2.1.

The automated upgrade process follows Ceph best practices.  For example:

* The upgrade order starts with managers, monitors, then other daemons.
* Each daemon is restarted only after Ceph indicates that the cluster
  will remain available.

.. note::

   The Ceph cluster health status is likely to switch to
   ``HEALTH_WARNING`` during the upgrade.

.. note:: 

   If a cluster host is or becomes unavailable the upgrade will be paused
   until it is restored.

.. note::

   When the PG autoscaler mode for **any** pool is set to ``on``, we recommend
   disabling the autoscaler for the duration of the upgrade.  This is so that
   PG splitting or merging in the middle of an upgrade does not unduly delay
   upgrade progress.  In a very large cluster this could easily increase the
   time to complete by a day or more, especially if the upgrade happens to
   change PG autoscaler behavior by e.g. changing the default value
   of :confval:`mon_target_pg_per_osd`.

   .. prompt:: bash #

     ceph osd pool set noautoscale
     # Perform the upgrade
     ceph osd pool unset noautoscale
   
   When pausing autoscaler activity in this fashion, the existing values for
   each pool's mode, ``off``, ``on``, or ``warn``, are expected to remain.
   If the new release changes the above target value, there may be splitting
   or merging of PGs when unsetting after the upgrade.


Upgrade image pre-distribution
==============================

On clusters with many hosts and large container images, pulling the target
image from a registry on each host during the upgrade can add significant
time. This is especially noticeable during MDS upgrades when CephFS may
already be offline while a host still downloads the image.

When enabled, cephadm pre-distributes the target image to in-scope hosts
**before any daemon is upgraded**. Configure this with
``mgr/cephadm/upgrade_image_mirror_method``:

``''`` / ``none`` (default)
  Disabled. Upgrade behaves as before (per-host pull on demand during
  daemon upgrade).

``registry``
  Pull in parallel on each in-scope host from the cluster registry (Harbor,
  Quay, etc.). Use this when every node already has registry connectivity.
  Target image digests and Ceph version are learned from those parallel
  pulls, so there is no extra serial "first pull" on a single host before
  the batch. Hosts that already have the target image are skipped. If
  pre-distribution fails on any host, the upgrade is paused before any
  daemon is upgraded. Staggered upgrades pre-pull only on hosts in the
  current upgrade scope.

Enable pre-distribution:

.. prompt:: bash #

   ceph config set mgr mgr/cephadm/upgrade_image_mirror_method registry

Disable again (either form):

.. prompt:: bash #

   ceph config set mgr mgr/cephadm/upgrade_image_mirror_method none
   # or clear to empty
   ceph config set mgr mgr/cephadm/upgrade_image_mirror_method ''

Optional tuning:

.. prompt:: bash #

   ceph config set mgr mgr/cephadm/upgrade_image_mirror_max_parallel 8


Starting the upgrade
====================

.. note::
   .. note::
      `Staggered Upgrade`_ of the Monitors and Managers may be necessary to use
      the below CephFS upgrade feature.

   Cephadm by default reduces ``max_mds`` to ``1``. This can be disruptive for large
   scale CephFS deployments because the cluster cannot quickly reduce active MDS(s)
   to `1` and a single active MDS cannot easily handle the load of all clients
   even for a short time. Therefore, to upgrade MDS(s) without reducing ``max_mds``,
   the ``fail_fs`` option can to be set to ``true`` (default value is ``false``) prior
   to initiating the upgrade:

   .. prompt:: bash #

      ceph config set mgr mgr/orchestrator/fail_fs true

   This would:
               #. Fail CephFS filesystems, bringing active MDS daemon(s) to
                  ``up:standby`` state.

               #. Upgrade MDS daemons safely.

               #. Bring CephFS filesystems back up, bringing the state of active
                  MDS daemon(s) from ``up:standby`` to `up:active``.

   When a cluster has more than one CephFS filesystem, cephadm upgrades the
   MDS daemons one filesystem at a time by default. It prepares (fails, or
   scales down to ``max_mds 1``) a single filesystem, upgrades its MDS
   daemons, restores that filesystem, and only then proceeds to the next one.
   Because MDS daemons are upgraded serially in any case, this does not slow
   the upgrade down; it only narrows the disruption to one filesystem at a
   time instead of taking every filesystem offline simultaneously. One
   exception: if an MDS daemon is found to be actively serving a filesystem
   other than its own service's (a standby takeover), both filesystems are
   prepared together, as restarting an active MDS of an unprepared
   filesystem would be unsafe. To restore the previous behavior of preparing
   all filesystems at once, set:

   .. prompt:: bash #

      ceph config set mgr mgr/cephadm/upgrade_fs_one_at_a_time false

   .. _cephadm-upgrade-mds-staged:

   **Staged MDS switch (shorter outage with ``fail_fs``)**

   With ``fail_fs = true`` the filesystem stays failed while cephadm redeploys
   its MDS daemons one after the other. Most of each redeployment does not
   need the filesystem to be down: cephadm only writes new unit files and
   restarts a container. To keep the filesystem down for as little as one
   container restart, enable the staged switch:

   .. prompt:: bash #

      ceph config set mgr mgr/orchestrator/fail_fs true
      ceph config set mgr mgr/cephadm/upgrade_mds_staged true

   For each filesystem, cephadm then:

               #. *stages* the new deployment on every MDS host while the
                  filesystem is still serving clients (new config, keyring
                  and unit files written next to the live ones, target image
                  pulled and executed once with ``--version``),

               #. flushes the MDS journals, disables standby-replay and
                  fails the filesystem,

               #. switches every MDS daemon to its staged unit files, all
                  hosts in parallel (one ``systemctl stop`` / ``start``
                  each),

               #. waits until the monitors report every daemon back as
                  ``up:standby`` with the target version, and

               #. sets the filesystem joinable again and restores
                  standby-replay.

   Anything that can go wrong with the image (pull, registry, uid lookup)
   goes wrong in step 1, with the filesystem still up. If a daemon does not
   come back on the target version in step 4, every daemon is switched back
   to its previous unit files, the filesystem is re-joined on the previous
   release and the upgrade is paused with an ``UPGRADE_MDS_SWITCH_FAILED``
   warning.

   Related options:

   * ``mgr/cephadm/upgrade_mds_staged_switch_timeout`` (default ``120``
     seconds): how long to wait in step 4 before rolling back.
   * ``mgr/cephadm/upgrade_mds_staged_max_parallel`` (default ``16``): how
     many hosts to stage or switch at once.
   * ``mgr/cephadm/upgrade_mds_fail_unhealthy_fs`` (default ``false``): on
     releases whose ``fs fail`` refuses a filesystem with MDS health
     warnings, retry with ``--yes-i-really-mean-it``.

   When a cluster has other standby MDS daemons pinned to the filesystem
   (``mds_join_fs``) that cephadm does not manage, they must run the target
   release before the filesystem is re-joined, or the monitors could hand a
   rank to an older daemon and then refuse the upgraded ones. cephadm checks
   this and rolls back rather than re-joining in that case. With several
   filesystems, consider ``ceph fs set <fs> refuse_standby_for_another_fs
   true`` so that standbys of one filesystem do not take ranks in another
   while it is being upgraded.

   To test the MDS phase alone on a cluster whose monitors and OSDs already
   run a compatible release, the upgrade order check of a filtered upgrade
   (``--daemon-types``, ``--services``, ``--hosts``) can be bypassed with
   ``ceph config set mgr mgr/cephadm/upgrade_skip_order_check true``; the
   active mgr must still be on the target image. This is a testing aid, not
   something to leave enabled.

Before you use cephadm to upgrade Ceph, verify that all hosts are currently online and that your cluster is healthy by running the following command:

.. prompt:: bash #

   ceph -s

To upgrade to a specific release, run a command of the following form:

.. prompt:: bash #

  ceph orch upgrade start --ceph-version <version>

For example, to upgrade to v16.2.6, run a command of the following form:

.. prompt:: bash #

  ceph orch upgrade start --ceph-version 16.2.6

.. note::

    From version v16.2.6 the Docker Hub registry is no longer used, so if you use Docker you have to point it to the image in the quay.io registry:

.. prompt:: bash #

  ceph orch upgrade start --image quay.io/ceph/ceph:v16.2.6


Monitoring the upgrade
======================

Determine (1) whether an upgrade is in progress and (2) which version the
cluster is upgrading to by running the following command:

.. prompt:: bash #

  ceph orch upgrade status

Watching the progress bar during a Ceph upgrade
-----------------------------------------------

During the upgrade, a progress bar is visible in the ceph status output. It
looks like this:

.. code-block:: console

  # ceph -s

  [...]
    progress:
      Upgrade to docker.io/ceph/ceph:v15.2.1 (00h 20m 12s)
        [=======.....................] (time remaining: 01h 43m 31s)

Watching the cephadm log during an upgrade
------------------------------------------

Watch the cephadm log by running the following command:

.. prompt:: bash #

  ceph -W cephadm


Canceling an upgrade
====================

You can stop the upgrade process at any time by running the following command:

.. prompt:: bash #

  ceph orch upgrade stop

Post upgrade actions
====================

In case the new version is based on ``cephadm``, once done with the upgrade the user
has to update the ``cephadm`` package (or ceph-common package in case the user
doesn't use ``cephadm shell``) to a version compatible with the new version.

Potential problems
==================


Error: ENOENT: Module not found
-------------------------------

The message ``Error ENOENT: Module not found`` appears in response to the command ``ceph orch upgrade status`` if the orchestrator has crashed:

.. prompt:: bash #

   ceph orch upgrade status

::

   Error ENOENT: Module not found

This is possibly caused by invalid JSON in a mgr config-key.
See `Redmine tracker Issue #67329 <https://tracker.ceph.com/issues/67329>`_
and `this discussion on the ceph-users mailing list <https://www.spinics.net/lists/ceph-users/msg83667.html>`_.

UPGRADE_NO_STANDBY_MGR
----------------------

This alert (``UPGRADE_NO_STANDBY_MGR``) means that Ceph does not detect an
active standby Manager daemon. In order to proceed with the upgrade, Ceph
requires an active standby Manager daemon (which you can think of in this
context as "a second manager").

You can ensure that Cephadm is configured to run two (or more) Managers by
running the following command:

.. prompt:: bash #

  ceph orch apply mgr 2  # or more

You can check the status of existing Manager daemons by running the following
command:

.. prompt:: bash #

  ceph orch ps --daemon-type mgr

If an existing Manager daemon has stopped, you can try to restart it by running the
following command: 

.. prompt:: bash #

  ceph orch daemon restart <name>

UPGRADE_FAILED_PULL
-------------------

This alert (``UPGRADE_FAILED_PULL``) means that Ceph was unable to pull the
container image for the target version. This can happen if you specify a
version or container image that does not exist (e.g. "1.2.3"), or if the
container registry can not be reached by one or more hosts in the cluster.

To cancel the existing upgrade and to specify a different target version, run
the following commands: 

.. prompt:: bash #

  ceph orch upgrade stop
  ceph orch upgrade start --ceph-version <version>


Using customized container images
=================================

For most users, upgrading requires nothing more complicated than specifying the
Ceph version to which to upgrade.  In such cases, cephadm locates the specific
Ceph container image to use by combining the ``container_image_base``
configuration option (default: ``docker.io/ceph/ceph``) with a tag of
``vX.Y.Z``.

But it is possible to upgrade to an arbitrary container image, if that's what
you need. For example, the following command upgrades to a development build:

.. prompt:: bash #

  ceph orch upgrade start --image quay.ceph.io/ceph-ci/ceph:recent-git-branch-name

For more information about available container images, see :ref:`containers`.

Staggered Upgrade
=================

Some users may prefer to upgrade components in phases rather than all at once.
The upgrade command, starting in 16.2.11 and 17.2.1 allows parameters
to limit which daemons are upgraded by a single upgrade command. The options in
include ``daemon_types``, ``services``, ``hosts`` and ``limit``. ``daemon_types``
takes a comma-separated list of daemon types and will only upgrade daemons of those
types. ``services`` is mutually exclusive with ``daemon_types``, only takes services
of one type at a time (e.g. can't provide an OSD and RGW service at the same time), and
will only upgrade daemons belonging to those services. ``hosts`` can be combined
with ``daemon_types`` or ``services`` or provided on its own. The ``hosts`` parameter
follows the same format as the command line options for :ref:`orchestrator-cli-placement-spec`.
``limit`` takes an integer > 0 and provides a numerical limit on the number of
daemons cephadm will upgrade. ``limit`` can be combined with any of the other
parameters. For example, if you specify to upgrade daemons of type osd on host
Host1 with ``limit`` set to 3, cephadm will upgrade (up to) 3 osd daemons on
Host1.

Example: specifying daemon types and hosts:

.. prompt:: bash #

  ceph orch upgrade start --image <image-name> --daemon-types mgr,mon --hosts host1,host2

Example: specifying services and using limit:

.. prompt:: bash #

  ceph orch upgrade start --image <image-name> --services rgw.example1,rgw.example2 --limit 2

.. note::

   Cephadm strictly enforces an order to the upgrade of daemons that is still present
   in staggered upgrade scenarios. The current upgrade ordering is
   ``mgr -> mon -> crash -> osd -> mds -> rgw -> rbd-mirror -> cephfs-mirror -> iscsi -> nfs``.
   If you specify parameters that would upgrade daemons out of order, the upgrade
   command will block and note which daemons will be missed if you proceed.

.. note::

  Upgrade commands with limiting parameters will validate the options before beginning the
  upgrade, which may require pulling the new container image. Do not be surprised
  if the upgrade start command takes a while to return when limiting parameters are provided.

.. note::

   In staggered upgrade scenarios (when a limiting parameter is provided) monitoring
   stack daemons including Prometheus and node-exporter are refreshed after the Manager
   daemons have been upgraded. Do not be surprised if Manager upgrades thus take longer
   than expected. Note that the versions of monitoring stack daemons may not change between
   Ceph releases, in which case they are only redeployed.

Upgrading to a version that supports staggered upgrade from one that doesn't
----------------------------------------------------------------------------

While upgrading from a version that already supports staggered upgrades the process
simply requires providing the necessary arguments. However, if you wish to upgrade
to a version that supports staggered upgrade from one that does not, there is a
workaround. It requires first manually upgrading the Manager daemons and then passing
the limiting parameters as usual.

.. warning::
  Make sure you have multiple running mgr daemons before attempting this procedure.

To start with, determine which Manager is your active one and which are standby. This
can be done in a variety of ways such as looking at the ``ceph -s`` output. Then,
manually upgrade each standby mgr daemon with:

.. prompt:: bash #

  ceph orch daemon redeploy mgr.example1.abcdef --image <new-image-name>

.. note::

   If you are on a very early version of cephadm (early Octopus) the ``orch daemon redeploy``
   command may not have the ``--image`` flag. In that case, you must manually set the
   Manager container image ``ceph config set mgr container_image <new-image-name>`` and then
   redeploy the Manager ``ceph orch daemon redeploy mgr.example1.abcdef``

At this point, a Manager fail over should allow us to have the active Manager be one
running the new version.

.. prompt:: bash #

  ceph mgr fail

Verify the active Manager is now one running the new version. To complete the Manager
upgrading:

.. prompt:: bash #

  ceph orch upgrade start --image <new-image-name> --daemon-types mgr

You should now have all your Manager daemons on the new version and be able to
specify the limiting parameters for the rest of the upgrade.
