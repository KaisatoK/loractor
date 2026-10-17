#!/bin/bash

# Seed the SereneDB log tables through the loghub loader (backend.db.loghub).
#
# This script takes NO arguments. It loads two local corpora, in this order:
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
# log tables. The corpora are loaded with --no-embed (no embedding model needed).
# Each corpus run drops and rebuilds its table. BGL is large and takes a long
# time. A missing source file stops the script with the loader's message naming
# what to add, so place the .log files (gitignored) before running.
#
# Needs SereneDB on localhost:7890 (see SERENEDB_* in .env). Usage:
#
#   bash ./setup.sh

set -e

if command -v uv &> /dev/null; then
    run=(uv run python)
else
    run=(python)
fi

"${run[@]}" -m backend.db.ownership
"${run[@]}" -m backend.db.loghub openstack --no-embed
"${run[@]}" -m backend.db.loghub bgl --no-embed
