#!/usr/bin/env bash
#
# Copyright (C) 2026 Clyso GmbH
#
# Author: Frederic Nass <frederic.nass@clyso.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Library Public License as published by
# the Free Software Foundation; either version 2, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Library Public License for more details.
#
source $CEPH_ROOT/qa/standalone/ceph-helpers.sh

function run() {
    local dir=$1
    shift

    export CEPH_MON="127.0.0.1:7161" # git grep '\<7161\>' : there must be only one
    export CEPH_ARGS
    CEPH_ARGS+="--fsid=$(uuidgen) --auth-supported=none "
    CEPH_ARGS+="--mon-host=$CEPH_MON "

    local funcs=${@:-$(set | sed -n -e 's/^\(TEST_[0-9a-z_]*\) .*/\1/p')}
    for func in $funcs ; do
        setup $dir || return 1
        $func $dir || return 1
        teardown $dir || return 1
    done
}

function count_remapped() {
    ceph pg dump pgs_brief 2>/dev/null | grep -c remapped
    return 0
}

function count_upmap_items() {
    ceph osd dump -f json 2>/dev/null | jq '.pg_upmap_items | length'
}

function setup_cluster() {
    local dir=$1

    run_mon $dir a || return 1
    run_mgr $dir x || return 1
    for id in 0 1 2 3 ; do
        run_osd $dir $id || return 1
    done
    ceph osd set-require-min-compat-client luminous || return 1
    ceph balancer off || return 1
    ceph osd pool create foo 32 32 || return 1
    ceph osd pool set foo size 2 || return 1
    wait_for_clean || return 1
}

function TEST_progressive_data_movement_auto_pin() {
    local dir=$1

    setup_cluster $dir || return 1
    ceph config set mon mon_osd_prefer_progressive_data_movement true || return 1

    # adding an osd would normally remap a bunch of pgs and trigger
    # backfill; with progressive data movement enabled, the mon must
    # instead pin the pgs to their current placement with pg_upmap_items
    # entries, in the same osdmap epoch
    run_osd $dir 4 || return 1

    wait_for_clean || return 1
    local items=$(count_upmap_items)
    test "$items" -gt 0 || return 1
    local remapped=$(count_remapped)
    test "$remapped" -eq 0 || return 1
}

function TEST_pin_remapped_pgs_command() {
    local dir=$1

    setup_cluster $dir || return 1

    # with the option off (default), adding an osd remaps pgs as usual;
    # norebalance keeps them in that state
    ceph osd set norebalance || return 1
    run_osd $dir 4 || return 1

    local remapped=0
    for i in $(seq 1 60) ; do
        remapped=$(count_remapped)
        test "$remapped" -gt 0 && break
        sleep 1
    done
    test "$remapped" -gt 0 || return 1

    # 'osd pin-remapped-pgs' must pin them back to their acting set and
    # make them active+clean without any data movement
    ceph osd pin-remapped-pgs || return 1

    local items=$(count_upmap_items)
    test "$items" -gt 0 || return 1

    ceph osd unset norebalance || return 1
    wait_for_clean || return 1
    remapped=$(count_remapped)
    test "$remapped" -eq 0 || return 1
}

main osd-progressive-data-movement "$@"
