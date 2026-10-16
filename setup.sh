#!/bin/bash

# Seed the SereneDB log tables through the loghub loader (backend.db.loghub).
#
# This script takes NO arguments. It loads two local corpora, in this order:
#
#   openstack  one table from three nova logs, loaded one after another:
#                src/backend/data/openstack_normal1.log
#                src/backend/data/openstack_normal2.log
#                src/backend/data/openstack_abnormal.log
#   bgl        the Blue Gene/L RAS log, 4.7M rows / 743 MB, from
#                src/backend/data/BGL.log  (or the path in BGL_LOG_PATH)
#
# Both are loaded with --no-embed (no embedding model needed). Each run drops
# and rebuilds the table. BGL is large and takes a long time. A missing source
# file stops the script with the loader's message naming it, so place the .log
# files (gitignored) before running.
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

"${run[@]}" -m backend.db.loghub openstack --no-embed
"${run[@]}" -m backend.db.loghub bgl --no-embed
