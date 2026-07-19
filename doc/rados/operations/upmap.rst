.. _upmap:

=======================================
Using pg-upmap
=======================================

In Luminous v12.2.z and later releases, there is a *pg-upmap* exception table
in the OSDMap that allows the cluster to explicitly map specific PGs to
specific OSDs. This allows the cluster to fine-tune the data distribution to,
in most cases, uniformly distribute PGs across OSDs.

However, there is an important caveat when it comes to this new feature: it
requires all clients to understand the new *pg-upmap* structure in the OSDMap.

Online Optimization
===================

Enabling
--------

In order to use ``pg-upmap``, the cluster cannot have any pre-Luminous clients.
By default, new clusters enable the *balancer module*, which makes use of
``pg-upmap``. If you want to use a different balancer or you want to make your
own custom ``pg-upmap`` entries, you might want to turn off the balancer in
order to avoid conflict: 

.. prompt:: bash $

   ceph balancer off

To allow use of the new feature on an existing cluster, you must restrict the
cluster to supporting only Luminous (and newer) clients.  To do so, run the
following command:

.. prompt:: bash $

   ceph osd set-require-min-compat-client luminous

This command will fail if any pre-Luminous clients or daemons are connected to
the monitors. To see which client versions are in use, run the following
command:

.. prompt:: bash $

   ceph features

See :ref:`require_min_compat_client` for what the
``set-require-min-compat-client`` command does and how to choose an
appropriate release argument.

Balancer Module
---------------

The `balancer` module for ``ceph-mgr`` will automatically balance the number of
PGs per OSD. See :ref:`balancer`

Offline Optimization
====================

Upmap entries are updated with an offline optimizer that is built into the
:ref:`osdmaptool`.

#. Grab the latest copy of your osdmap:

   .. prompt:: bash $

      ceph osd getmap -o om

#. Run the optimizer:

   .. prompt:: bash $

      osdmaptool om --upmap out.txt [--upmap-pool <pool>] \
      [--upmap-max <max-optimizations>] \
      [--upmap-deviation <max-deviation>] \
      [--upmap-active]

   It is highly recommended that optimization be done for each pool
   individually, or for sets of similarly utilized pools. You can specify the
   ``--upmap-pool`` option multiple times. "Similarly utilized pools" means
   pools that are mapped to the same devices and that store the same kind of
   data (for example, RBD image pools are considered to be similarly utilized;
   an RGW index pool and an RGW data pool are not considered to be similarly
   utilized).

   The ``max-optimizations`` value determines the maximum number of upmap
   entries to identify. The default is `10` (as is the case with the
   ``ceph-mgr`` balancer module), but you should use a larger number if you are
   doing offline optimization.  If it cannot find any additional changes to
   make (that is, if the pool distribution is perfect), it will stop early.

   The ``max-deviation`` value defaults to `5`. If an OSD's PG count varies
   from the computed target number by no more than this amount it will be
   considered perfect.

   The ``--upmap-active`` option simulates the behavior of the active balancer
   in upmap mode. It keeps cycling until the OSDs are balanced and reports how
   many rounds have occurred and how long each round takes. The elapsed time
   for rounds indicates the CPU load that ``ceph-mgr`` consumes when it computes
   the next optimization plan.

#. Apply the changes:

   .. prompt:: bash $

      source out.txt

   In the above example, the proposed changes are written to the output file
   ``out.txt``. The commands in this procedure are normal Ceph CLI commands
   that can be run in order to apply the changes to the cluster.

The above steps can be repeated as many times as necessary to achieve a perfect
distribution of PGs for each set of pools.

To see some (gory) details about what the tool is doing, you can pass
``--debug-osd 10`` to ``osdmaptool``. To see even more details, pass
``--debug-crush 10`` to ``osdmaptool``.

.. _upmap_progressive_data_movement:

Progressive Data Movement
=========================

By default, any change that alters CRUSH placement -- adding OSDs or hosts,
draining OSDs by reducing their CRUSH weights, changing a pool's CRUSH rule,
or increasing a pool's ``pgp_num`` -- immediately remaps the affected PGs and
starts backfill. On large clusters, this can put a significant fraction of
the data into a ``remapped``/``misplaced`` state at once, and the resulting
backfill can take days while degrading client performance.

Setting ``mon_osd_prefer_progressive_data_movement`` to ``true`` changes this
behavior: whenever such a change would remap PGs, the monitor creates
``pg_upmap_items`` entries *in the same OSDMap epoch* that pin the affected
PGs to their current (clean) placement. PGs therefore stay ``active+clean``
and no immediate data movement occurs. The :ref:`balancer <balancer>` in
``upmap`` mode then gradually removes these entries, moving data toward its
intended placement at the pace bounded by ``target_max_misplaced_ratio``
(and, because PGs are clean rather than remapped, this happens within the
balancer's normal schedule). This is the native, atomic equivalent of the
``upmap-remapped.py`` and ``pgremapper`` community tools:

.. prompt:: bash $

   ceph config set mon mon_osd_prefer_progressive_data_movement true

To make PGs that are *already* remapped ``active+clean`` again (for example,
after a change made before the option was enabled), the same pinning logic
can be applied on demand:

.. prompt:: bash $

   ceph osd pin-remapped-pgs

Requirements and limitations:

* ``require-min-compat-client`` must be ``luminous`` or newer, as with any
  use of ``pg-upmap``.
* The balancer should be enabled and in ``upmap`` mode; otherwise, pinned
  PGs simply remain where they are.
* To drain OSDs progressively, reduce their CRUSH weight to a small non-zero
  value (for example ``0.001``) rather than ``0``, and do not mark them
  ``out``: upmap entries that point to out or zero-weight OSDs are invalid
  and are automatically cancelled, in which case the affected PGs backfill
  immediately as usual.
* Pinning is skipped for degraded or undersized PGs (redundancy must be
  restored by recovery or backfill, not deferred), and when OSDs come back
  ``up`` or ``in`` (returning OSDs must be repopulated).
* PG merges (``pg_num`` decreases) are never pinned: a merge source must be
  colocated with its merge target before merging, so this data movement
  cannot be deferred. It is already throttled by ``target_max_misplaced_ratio``.
* After a CRUSH rule change, pins whose placement would violate the new rule
  (for example, two replicas in the same failure domain after switching from
  ``host`` to ``rack`` failure domain) are automatically cancelled and those
  PGs backfill immediately: only the movement that the new rule strictly
  requires happens at once.
* A pinned cluster reports ``HEALTH_OK`` even though data is not yet where
  CRUSH wants it. Use ``ceph osd dump | grep pg_upmap_items | wc -l`` to
  monitor how many pins remain.
