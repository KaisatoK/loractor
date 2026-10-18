#!/bin/bash

# One command to take a fresh Linux checkout to a working chatbot:
#
#   1. uv                 installed if missing
#   2. dependencies       uv sync
#   3. .env               created from .env.example, then checked
#   4. SereneDB           started (native binary, Docker, or one you already run)
#   5. readiness          waited for, and SERENEDB_PORT written to .env
#   6. server state       memory ceiling held, embedding secret created PERSISTENT
#   7. corpora            ownership table + log corpora loaded (skipping loaded ones)
#   8. verification       the state printed back, including what the model returns
#
# Every step is idempotent: re-running after a reboot or a recreated container
# is the intended way to recover an instance. A corpus that is already loaded is
# left alone, because a load drops and rebuilds its table and BGL takes a long
# time.
#
# Usage:
#   bash ./bootstrap.sh                        # everything; SereneDB as a native binary
#   bash ./bootstrap.sh --db docker            # run SereneDB in Docker instead
#   bash ./bootstrap.sh --db existing          # use the server SERENEDB_* already points at
#   bash ./bootstrap.sh --datasets "openstack" # load only these loader datasets
#   bash ./bootstrap.sh --samples              # loghub's 2,000-row samples, not the full corpora
#   bash ./bootstrap.sh --skip-data            # set the server up, load nothing
#   bash ./bootstrap.sh --embed                # also build template embeddings (costs provider quota)
#   bash ./bootstrap.sh --force-reload         # rebuild corpora even if already loaded
#   bash ./bootstrap.sh --yes                  # never prompt (needed for CI / non-interactive)
#   bash ./bootstrap.sh --no-probe             # skip the one embedding call in step 8
#
# Two steps download and execute a vendor installer (uv from astral.sh, SereneDB
# from linux.serenedb.com). Both print the exact command first and ask, unless
# --yes is given. Use --db existing and a pre-installed uv to avoid them.
#
# Linux only: the SereneDB shell installer supports Linux, and on other systems
# it stops and points at Docker -- use --db docker there.

# This script uses bash features (arrays, [[, BASH_SOURCE). If it was started
# with a POSIX shell such as dash (`sh ./bootstrap.sh`), re-exec under bash so
# the user does not get cryptic syntax errors.
if [ -z "${BASH_VERSION:-}" ]; then
    if command -v bash >/dev/null 2>&1; then
        exec bash "$0" "$@"
    fi
    echo "bootstrap.sh needs bash; run it as:  bash ./bootstrap.sh" >&2
    exit 1
fi

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_root"

db_mode=native
datasets_arg=""
use_samples=0
skip_data=0
do_embed=0
force_reload=0
assume_yes=0
probe_embedding=1

while [ $# -gt 0 ]; do
    case "$1" in
        --db) db_mode="${2:-}"; shift 2 ;;
        --db=*) db_mode="${1#*=}"; shift ;;
        --datasets) datasets_arg="${2:-}"; shift 2 ;;
        --datasets=*) datasets_arg="${1#*=}"; shift ;;
        --samples) use_samples=1; shift ;;
        --skip-data) skip_data=1; shift ;;
        --embed) do_embed=1; shift ;;
        --force-reload) force_reload=1; shift ;;
        --no-probe) probe_embedding=0; shift ;;
        --yes|-y) assume_yes=1; shift ;;
        -h|--help) sed -n '3,35p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "bootstrap.sh: unknown option: $1" >&2; exit 2 ;;
    esac
done

case "$db_mode" in
    native|docker|existing) ;;
    *) echo "bootstrap.sh: --db must be native, docker or existing" >&2; exit 2 ;;
esac

step=0
step_total=8
say() { printf '\n==> [%d/%d] %s\n' "$step" "$step_total" "$1"; }
note() { printf '    %s\n' "$1"; }
warn() { printf '    warning: %s\n' "$1" >&2; }
die() { printf '\nbootstrap.sh: %s\n' "$1" >&2; exit 1; }

confirm() {
    [ "$assume_yes" = 1 ] && return 0
    if [ ! -t 0 ]; then
        warn "non-interactive shell; pass --yes to allow this"
        return 1
    fi
    local reply
    read -r -p "    $1 [y/N] " reply
    case "$reply" in [yY]*) return 0 ;; *) return 1 ;; esac
}

# Set a key in .env without disturbing the rest of the file.
set_env_key() {
    local key="$1" value="$2"
    if grep -q "^${key}=" .env 2>/dev/null; then
        local current
        current="$(sed -n "s/^${key}=//p" .env | head -1)"
        [ "$current" = "$value" ] && return 0
        python3 - "$key" "$value" <<'PY'
import pathlib, re, sys
key, value = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env")
text = p.read_text(encoding="utf-8")
p.write_text(re.sub(rf"(?m)^{re.escape(key)}=.*$", f"{key}={value}", text), encoding="utf-8")
PY
        note "updated ${key}=${value} in .env"
    else
        printf '%s=%s\n' "$key" "$value" >> .env
        note "added ${key}=${value} to .env"
    fi
}

# Read a key from .env. Strips an optional "export " prefix and a single pair
# of surrounding single/double quotes, so a quoted value (KEY="value") is read
# as its bare value -- matching how python-dotenv parses the same file.
env_value() {
    local raw
    raw="$(sed -n "s/^[[:space:]]*\(export[[:space:]]\+\)\{0,1\}$1=//p" .env 2>/dev/null | head -1)"
    # strip one matching pair of surrounding quotes
    case "$raw" in
        \"*\") raw="${raw#\"}"; raw="${raw%\"}" ;;
        \'*\') raw="${raw#\'}"; raw="${raw%\'}" ;;
    esac
    printf '%s' "$raw"
}


# --- 1. preflight and uv ---------------------------------------------------

step=1; say "Checking the host and installing uv"

[ "$(uname -s)" = "Linux" ] || warn "this script targets Linux; on $(uname -s) use --db docker"
command -v curl >/dev/null || die "curl is required"
command -v python3 >/dev/null || die "python3 is required (for small .env edits)"

export PATH="$HOME/.local/bin:$PATH"
if command -v uv >/dev/null; then
    note "uv present: $(uv --version)"
else
    note "uv is missing; the official installer is:"
    note "    curl -LsSf https://astral.sh/uv/install.sh | sh"
    confirm "download and run it?" || die "uv is required; install it and re-run"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    command -v uv >/dev/null || die "uv still not on PATH; add \$HOME/.local/bin to it"
    note "uv installed: $(uv --version)"
    note "add this to ~/.bashrc so new shells find it:"
    note "    export PATH=\"\$HOME/.local/bin:\$PATH\""
fi


# --- 2. dependencies -------------------------------------------------------

step=2; say "Installing dependencies (uv sync)"
uv sync
note "virtualenv ready at .venv"


# --- 3. .env ---------------------------------------------------------------

step=3; say "Checking .env"

if [ ! -f .env ]; then
    cp .env.example .env
    note "created .env from .env.example"
fi

missing_keys=()
for key in GROQ_API_KEY DEEPSEEK_API_KEY GOOGLE_API_KEY TYPESAFE_API_KEY; do
    value="$(env_value "$key")"
    case "$value" in
        ""|"..."|"your-"*|"<"*) missing_keys+=("$key") ;;
    esac
done
if [ "${#missing_keys[@]}" -gt 0 ]; then
    warn "no value yet for: ${missing_keys[*]}"
    note "the database setup below does not need them, but the backend reads all"
    note "four at import, so ./backend.sh will not start until they are set."
fi

google_key="$(env_value GOOGLE_API_KEY)"
want_secret=$do_embed
case "$google_key" in
    ""|"..."|"your-"*|"<"*) want_secret=0 ;;
esac
[ "$want_secret" = 1 ] || warn "GOOGLE_API_KEY unset: skipping the embedding secret (search stays lexical)"


# --- 4. SereneDB -----------------------------------------------------------

step=4; say "Starting SereneDB (--db $db_mode)"

serenedb_port="$(env_value SERENEDB_PORT)"
[ -n "$serenedb_port" ] || serenedb_port=7890

case "$db_mode" in
existing)
    note "using the server SERENEDB_* points at (${serenedb_port})"
    ;;
native)
    # If a server is already answering on whatever SERENEDB_PORT currently
    # points at, adopt it rather than install/start a native one and overwrite
    # the port. This is what makes a pre-existing instance (e.g. a Docker
    # container on a non-default host port) just work without --db existing.
    serenedb_host="$(env_value SERENEDB_HOST)"
    [ -n "$serenedb_host" ] || serenedb_host=localhost
    set_env_key SERENEDB_HOST "$serenedb_host"
    if SERENEDB_PORT="$serenedb_port" uv run python -c "from backend.db.provision import server_is_up; raise SystemExit(0 if server_is_up() else 1)" 2>/dev/null; then
        note "a server is already answering on ${serenedb_port}; adopting it (SERENEDB_PORT left at $serenedb_port)"
        set_env_key SERENEDB_PORT "$serenedb_port"
    else
        if ! command -v serened >/dev/null; then
            note "serened is missing; SereneDB's Linux installer is:"
            note "    curl https://linux.serenedb.com | sh"
            confirm "download and run it?" || die "install SereneDB, or re-run with --db docker"
            curl https://linux.serenedb.com | sh
            export PATH="$HOME/.local/bin:$PATH"
            command -v serened >/dev/null || die "serened not on PATH; add the directory the installer printed"
        fi
        note "serened present"
        # The native server listens on 127.0.0.1:7890 and keeps its data
        # (including persistent secrets) in the directory it is given.
        data_dir="${SERENEDB_DATA_DIR:-$repo_root/.data/serenedb}"
        mkdir -p "$data_dir" .data
        set_env_key SERENEDB_HOST localhost
        set_env_key SERENEDB_PORT 7890
        serenedb_port=7890
        note "starting: serened $data_dir"
        nohup serened "$data_dir" >> .data/serened.log 2>&1 &
        echo $! > .data/serened.pid
        note "started (pid $(cat .data/serened.pid)); log: .data/serened.log"
        note "stop it with: kill \$(cat .data/serened.pid)"
    fi
    ;;
docker)
    command -v docker >/dev/null || die "docker is not installed; use --db native"
    if ! docker info >/dev/null 2>&1; then
        die "cannot talk to the Docker daemon. Add yourself to the docker group
    (sudo usermod -aG docker \$USER, then log out and in), run this with sudo,
    or use --db native, which needs no daemon and no root."
    fi
    set_env_key SERENEDB_HOST localhost
    set_env_key SERENEDB_PORT "$serenedb_port"
    if docker ps -a --format '{{.Names}}' | grep -qx serenedb; then
        # Adopt it rather than create one: docker-compose.yml pins
        # container_name, so a container left by a plain `docker run` would make
        # `docker compose up` fail on a name conflict. `docker start` is a no-op
        # on a container that is already running.
        note "reusing the existing serenedb container"
        docker start serenedb >/dev/null
        note "its published port is what counts here; docker ps shows the mapping"
    elif docker compose version >/dev/null 2>&1 && [ -f docker-compose.yml ]; then
        note "docker compose up -d (host port $serenedb_port, volume serenedb-data)"
        SERENEDB_PORT="$serenedb_port" docker compose up -d
    else
        note "docker run -d --name serenedb -p ${serenedb_port}:7890 -v serenedb-data:/var/lib/serenedb serenedb/serenedb"
        docker run -d --name serenedb \
            -p "${serenedb_port}:7890" \
            -v serenedb-data:/var/lib/serenedb \
            serenedb/serenedb >/dev/null
    fi
    note "host port is pinned to $serenedb_port, so it no longer moves when the container is recreated"
    ;;
esac


# --- 5. readiness ----------------------------------------------------------

step=5; say "Waiting for the server to answer a query"
uv run python -m backend.db.provision --wait 180


# --- 6. server-side state --------------------------------------------------

step=6; say "Holding the memory ceiling and creating the embedding secret"
if [ "$want_secret" = 1 ]; then
    uv run python -m backend.db.provision --memory --secret
    note "the secret is PERSISTENT, so it survives a restart of the server"
else
    uv run python -m backend.db.provision --memory
fi


# --- 7. corpora ------------------------------------------------------------

step=7; say "Loading the log corpora"

if [ "$skip_data" = 1 ]; then
    note "--skip-data: nothing loaded"
else
    if [ -n "$datasets_arg" ]; then
        read -r -a wanted <<< "$datasets_arg"
    elif [ "$use_samples" = 1 ]; then
        wanted=(openstack_2k bgl_2k)
    else
        wanted=(openstack bgl)
    fi

    # Fall back to the bundled 2,000-row samples when a raw loghub file is
    # absent, so a fresh clone loads *something* instead of failing.
    bgl_source="${BGL_LOG_PATH:-$(env_value BGL_LOG_PATH)}"
    [ -n "$bgl_source" ] || bgl_source="src/backend/data/BGL.log"
    resolved=()
    for dataset in "${wanted[@]}"; do
        case "$dataset" in
        openstack)
            if [ -f src/backend/data/openstack_abnormal.log ] \
               && [ -f src/backend/data/openstack_normal1.log ] \
               && [ -f src/backend/data/openstack_normal2.log ]; then
                resolved+=(openstack)
            elif [ -f src/backend/data/OpenStack_2k.log_structured.csv ]; then
                warn "the three OpenStack nova logs are missing; loading the bundled 2,000-row sample instead"
                resolved+=(openstack_2k)
            else
                warn "no OpenStack source at all; skipping it"
            fi
            ;;
        bgl)
            if [ -f "$bgl_source" ]; then
                resolved+=(bgl)
            elif [ -f src/backend/data/BGL_2k.log_structured.csv ]; then
                warn "BGL.log not found at $bgl_source; loading the bundled 2,000-row sample instead"
                warn "download BGL.log from loghub and set BGL_LOG_PATH for the full 4.7M-row corpus"
                resolved+=(bgl_2k)
            else
                warn "no BGL source at all; skipping it"
            fi
            ;;
        *) resolved+=("$dataset") ;;
        esac
    done

    if [ "${#resolved[@]}" -eq 0 ]; then
        warn "no loadable corpus found; put the loghub logs in src/backend/data/ and re-run"
    else
        if [ "$force_reload" = 1 ]; then
            to_load=("${resolved[@]}")
            note "--force-reload: rebuilding ${to_load[*]}"
        else
            if ! pending="$(uv run python -m backend.db.provision \
                    --pending-datasets "${resolved[@]}")"; then
                die "could not ask the database which corpora are loaded; it
    answered step 5 but failed this query. Re-run, or load explicitly with
    bash ./setup.sh ${resolved[*]}"
            fi
            to_load=()
            while IFS= read -r line; do
                [ -n "$line" ] && to_load+=("$line")
            done <<< "$pending"
            skipped=()
            for dataset in "${resolved[@]}"; do
                printf '%s\n' "${to_load[@]:-}" | grep -qx "$dataset" || skipped+=("$dataset")
            done
            [ "${#skipped[@]}" -gt 0 ] && note "already loaded, left alone: ${skipped[*]}"
        fi

        # The ownership table is cheap and is reloaded every run; it touches no
        # log table.
        uv run python -m backend.db.ownership

        if [ "${#to_load[@]}" -eq 0 ] || [ -z "${to_load[0]:-}" ]; then
            note "every requested corpus is already loaded"
        else
            for dataset in "${to_load[@]}"; do
                case "$dataset" in
                    bgl) note "loading bgl (4.7M rows; this takes a while)" ;;
                    *) note "loading $dataset" ;;
                esac
            done
            embed_flag=--no-embed
            if [ "$do_embed" = 1 ]; then
                embed_flag=--embed
                warn "--embed builds template embeddings: thousands of texts per corpus."
                warn "The provider quota is the binding constraint -- see README Limitations."
            fi
            bash ./setup.sh "$embed_flag" "${to_load[@]}"
        fi
    fi
fi


# --- 8. verification -------------------------------------------------------

step=8; say "Verifying"
if [ "$want_secret" = 1 ] && [ "$probe_embedding" = 1 ]; then
    uv run python -m backend.db.provision --report --probe-embedding
else
    uv run python -m backend.db.provision --report
fi

cat <<'NEXT'

Setup complete. Next:

    bash ./backend.sh      # FastAPI backend (needs all four provider keys)
    bash ./frontend.sh     # Streamlit UI
    uv run pytest          # the offline suite
    bash ./mcp_server.sh   # the MCP tool service, for an external agent

Re-run this script any time: it skips what is already done. After a reboot or a
recreated container, re-running restarts the server and re-checks the secret.
NEXT
