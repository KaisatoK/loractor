#!/bin/bash

# Seed the SereneDB log tables through the loghub loader (backend.db.loghub).
#
# With no datasets it loads the two local corpora, in this order:
#
#   openstack  one table from three nova logs, loaded one after another in
#              chronological order (so LineId order is time order):
#                src/backend/data/openstack_abnormal.log
#                src/backend/data/openstack_normal1.log
#                src/backend/data/openstack_normal2.log
#   bgl        the Blue Gene/L RAS log, 4.7M rows / 743 MB, from
#                src/backend/data/BGL.log  (or the path in BGL_LOG_PATH)
#
# The ownership CSV is loaded first into ``team_ownership``; it does not touch
# log tables. The corpora are loaded with --no-embed (no embedding model needed)
# unless ``--embed`` is given. Each corpus run drops and rebuilds its table. BGL
# is large and takes a long time. A missing source file stops the script with
# the loader's message naming what to add, so place the .log files (gitignored)
# before running.
#
# Needs SereneDB on localhost:7890 (see SERENEDB_* in .env). Usage:
#
#   bash ./setup.sh                           # ownership + both local corpora
#   bash ./setup.sh <dataset> [<dataset>...]  # ownership + only those corpora
#   bash ./setup.sh --embed [<dataset>...]    # also build template embeddings
#   bash ./setup.sh --external-index            # ensure first 5s is searchable
#
# ``--external-index`` is exclusive: it invokes backend.db.external and does
# not load local corpora. Datasets are the loader's ids: openstack, bgl,
# openstack_2k, bgl_2k.

set -e

if command -v uv &> /dev/null; then
    run=(uv run python)
else
    run=(python)
fi

embed=--no-embed
datasets=()
external_index=0
for arg in "$@"; do
    case "$arg" in
        --embed) embed="" ;;
        --no-embed) embed=--no-embed ;;
        --external-index) external_index=1 ;;
        -*) echo "setup.sh: unknown option: $arg" >&2; exit 2 ;;
        *) datasets+=("$arg") ;;
    esac
done

if [ "$external_index" -eq 1 ]; then
    if [ "${#datasets[@]}" -ne 0 ]; then
        echo "setup.sh: --external-index cannot be combined with dataset names" >&2
        exit 2
    fi
    "${run[@]}" -m backend.db.external
    exit 0
fi

if [ "${#datasets[@]}" -eq 0 ]; then
    datasets=(openstack bgl)
fi

"${run[@]}" -m backend.db.ownership
for dataset in "${datasets[@]}"; do
    "${run[@]}" -m backend.db.loghub "$dataset" $embed
done
