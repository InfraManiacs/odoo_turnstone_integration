#!/usr/bin/env bash
#
# Install the addon into a throwaway database and run its tests against the mock
# Turnstone server. One command, and no state left behind.
#
#   scripts/test-odoo.sh                                  # install + run every test
#   scripts/test-odoo.sh /turnstone_integration:TestThing  # --test-tags filter
#   scripts/test-odoo.sh --keep                           # leave the stack up
#   scripts/test-odoo.sh --resume                         # re-attach to a --keep run
#   scripts/test-odoo.sh --help
#
# --keep prints the id it used; --resume with OTI_TEST_RUN_ID=<that id> runs
# against the same stack and database again. Anything else that finds an
# existing volume refuses to start rather than adopt it.
#
# The exit status is Odoo's: 0 only if the module installed and every test
# passed. A test that fails, an addon that does not install, a filter that
# matches nothing, and a stack that does not come up are all non-zero — and
# each says which of them it was.
#
# Scope: tests tagged `standard` in *this* module. Without that filter Odoo's
# `--test-enable` runs every test of every module it just installed (base, mail,
# web, ...), and the Odoo image ships core test failures — 24 of them on
# 19.0-20260926 — so the run would be red for reasons nothing here can fix.
# Whatever is passed positionally replaces the default filter.
#
# Nothing here touches the developer stack. Every run gets its own compose
# project name and volume, both derived from a run id, so:
#
#   * it cannot resolve to the developer project or its db-data/odoo-data
#     volumes (`down -v` on that name would delete the developer's database);
#   * it cannot touch a database it did not create — the name is unique to this
#     run and the script aborts rather than reusing one that already exists;
#   * `down -v` can only remove this run's volume, because it is scoped to a
#     project name the script just invented, and the volume carries that name.
#
# Written for bash 3.2 (macOS /bin/bash): no associative arrays, no ${x,,},
# no mapfile.
#
# Environment:
#   OTI_TEST_RUN_ID        pin this run's id (must start oti_test_); with
#                          --resume, re-attach to a --keep run
#   OTI_TEST_ODOO_IMAGE    default odoo:19.0-20260926
#   OTI_TEST_REQUIRE_TESTS 1 = a run that collects no test is a failure
#   OTI_TEST_WAIT_TIMEOUT  seconds to wait for db+mock health, default 180
#   OTI_TEST_LOG_DIR       where the run log goes, default $TMPDIR
#   OTI_TEST_LOG           exact path for the run log
#   OTI_TEST_MOCK_LOG_LEVEL  mock server verbosity, default WARNING

set -euo pipefail

repo_root=$(cd -- "$(dirname -- "$0")/.." && pwd)
compose_file="$repo_root/docker-compose.test.yml"
odoo_image=${OTI_TEST_ODOO_IMAGE:-odoo:19.0-20260926}
module=turnstone_integration
wait_timeout=${OTI_TEST_WAIT_TIMEOUT:-180}

usage() {
    sed -n '2,28p' "$0" | sed 's/^# *//'
}

keep=0
resume=0
tags=""
while [ $# -gt 0 ]; do
    case "$1" in
        --keep) keep=1; shift ;;
        --resume) resume=1; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "test-odoo: unknown option '$1' (try --help)" >&2; exit 2 ;;
        *) tags=$1; shift ;;
    esac
done

command -v docker >/dev/null 2>&1 || { echo "test-odoo: docker is not on PATH" >&2; exit 2; }
docker compose version >/dev/null 2>&1 || { echo "test-odoo: 'docker compose' (v2) is required" >&2; exit 2; }
if [ ! -f "$compose_file" ]; then
    echo "test-odoo: $compose_file is missing; run this from a checkout of the repo." >&2
    exit 2
fi

# Two independent things keep a test run off the developer stack, and neither
# depends on guessing where the developer stack lives:
#
#  1. Structural: the run id always starts with `oti_test_`, so the project
#     name can never equal a directory-derived project name like
#     `odoo_turnstone_integration`. This is the real guarantee.
#  2. A tripwire below: refuse outright if the volume this run would create
#     already exists, because that volume is somebody else's data — a
#     developer's `…_db-data` included. `down -v` would then delete it.
#
# The tripwire matters because the file pins its volume to
# `${OTI_TEST_PROJECT}-db-data`: if the project name ever collided with the
# developer stack's, the two volumes would be the *same volume*.

# ---------------------------------------------------------------------------
# Identify this run. One id names the project, the volume and the database, so
# they can never be confused with another run's — or the developer's. Lowercase
# alphanumerics and underscores only: the id becomes a Postgres database name,
# and Odoo may hand that to Postgres without quoting it.
#
# OTI_TEST_RUN_ID pins the id (CI, or reproducing a --keep run). It still has
# to start with oti_test_, and the volume guard below still applies — pinning
# is how you *re-attach* to a run, not a way around the safety checks.
# ---------------------------------------------------------------------------
run_id=${OTI_TEST_RUN_ID:-oti_test_$(date -u +%Y%m%dt%H%M%Sz)_$$_${RANDOM}}
case "$run_id" in
    *[!a-z0-9_]*) echo "test-odoo: run id '$run_id' must be lowercase letters, digits and _ only." >&2; exit 2 ;;
esac
project=$run_id
database=$run_id
token="mock-${RANDOM}${RANDOM}-token"
log_dir=${OTI_TEST_LOG_DIR:-${TMPDIR:-/tmp}}
log_file=${OTI_TEST_LOG:-"${log_dir%/}/oti-test-${run_id}.log"}

case "$project" in
    oti_test_*) : ;;
    *)
        echo "test-odoo: refusing to run as compose project '$project' — run ids start with oti_test_." >&2
        exit 2 ;;
esac

# The volume name the compose file will pin for this run. If it already exists
# and the script is not resuming that same run (`--resume` below), it holds
# somebody else's data — a --keep run being debugged, or, the case this guard
# exists for, a name collision with the developer stack. Never adopt it, and
# never let `down -v` reach it.
volume_name="${project}-db-data"
if [ "$resume" != 1 ] && docker volume inspect "$volume_name" >/dev/null 2>&1; then
    echo "test-odoo: refusing to run: volume '$volume_name' already exists and is not this run's." >&2
    echo "test-odoo: nothing was started. To re-attach to that run use --resume; to discard it:" >&2
    echo "test-odoo:   docker volume rm $volume_name" >&2
    exit 2
fi

compose() {
    OTI_TEST_PROJECT="$project" OTI_TEST_TOKEN="$token" OTI_TEST_ODOO_IMAGE="$odoo_image" \
        docker compose -p "$project" -f "$compose_file" "$@"
}

# Everything left in this run's compose project: containers by project label,
# named volumes by label (compose labels the volume it created), and this run's
# volume by the explicit name the file pins it to. `down -v` is supposed to take
# all of it — this is how the script proves it did instead of assuming.
leftovers() {
    {
        docker ps -aq --filter "label=com.docker.compose.project=$project" 2>/dev/null || true
    } | sed '/^$/d'
}
leftover_volumes() {
    {
        docker volume ls -q --filter "label=com.docker.compose.project=$project" 2>/dev/null || true
        docker volume ls -q --filter "name=^${project}-db-data$" 2>/dev/null || true
    } | sed '/^$/d' | sort -u
}

cleanup() {
    status=$?
    set +e

    if [ "$keep" = 1 ]; then
        echo
        echo "test-odoo: --keep: leaving project $project up"
        echo "  run id:   $project"
        echo "  database: $database"
        echo "  psql:  OTI_TEST_PROJECT=$project OTI_TEST_TOKEN=$token docker compose -p $project -f $compose_file exec db psql -U odoo -d $database"
        echo "  shell: OTI_TEST_PROJECT=$project OTI_TEST_TOKEN=$token docker compose -p $project -f $compose_file run --rm --no-deps odoo odoo shell -d $database"
        echo "  rerun: OTI_TEST_RUN_ID=$project scripts/test-odoo.sh --resume"
        echo "  down:  OTI_TEST_PROJECT=$project OTI_TEST_TOKEN=$token docker compose -p $project -f $compose_file down -v --remove-orphans"
        return $status
    fi

    echo "test-odoo: tearing down project $project"
    compose down -v --remove-orphans >/dev/null 2>&1

    # Removal is asynchronous enough that an immediate check can race it. Give
    # it a few seconds before calling anything a leftover.
    n=0
    while [ "$n" -lt 10 ]; do
        if [ -z "$(leftovers)" ] && [ -z "$(leftover_volumes)" ]; then
            break
        fi
        sleep 1
        n=$((n + 1))
    done

    stray_containers=$(leftovers)
    stray_volumes=$(leftover_volumes)
    if [ -n "$stray_containers$stray_volumes" ]; then
        echo "test-odoo: ERROR: teardown left things behind" >&2
        [ -n "$stray_containers" ] && echo "  containers: $stray_containers" >&2
        [ -n "$stray_volumes" ] && echo "  volumes:    $stray_volumes" >&2
        [ "$status" -eq 0 ] && status=1
    else
        echo "test-odoo: torn down — nothing left in $project"
    fi
    return $status
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "test-odoo: project $project"
echo "test-odoo: image   $odoo_image"
echo "test-odoo: database $database (fresh; never reused)"
echo "test-odoo: log     $log_file"

# ---------------------------------------------------------------------------
# 1. Dependencies up, and the mock reachable *by service name* from Odoo.
# ---------------------------------------------------------------------------
compose up -d --wait --wait-timeout "$wait_timeout" db mock-turnstone

# The tests will talk to the mock as http://mock-turnstone:8099, so prove that
# name resolves and answers before spending time on an Odoo install. A failure
# here is a compose-network problem, and one clear line beats the same thing
# surfacing as a connection error inside a test 200 lines down.
if ! compose run --rm --no-deps --entrypoint python3 odoo \
        -c 'import sys, urllib.request
url = "http://mock-turnstone:8099/health"
try:
    with urllib.request.urlopen(url, timeout=5) as r:
        print("test-odoo: mock reachable at %s -> HTTP %s" % (url, r.status))
except Exception as exc:
    print("test-odoo: cannot reach the mock at %s from the Odoo container: %s" % (url, exc),
          file=sys.stderr)
    sys.exit(1)'; then
    echo "test-odoo: FAILED: the mock server is not reachable by service name." >&2
    exit 1
fi

# "Never touch a database not created by the script." The name carries this
# run's id, so an existing one means either a collision, a leftover from a
# --keep run, or a --resume of one. Only --resume may reuse it, and even then
# the script never drops it — tear-down removes the whole volume.
exists=$(compose exec -T db psql -U odoo -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname = '$database'" | tr -d '[:space:]')
if [ "$exists" = "1" ]; then
    if [ "$resume" != 1 ]; then
        echo "test-odoo: database '$database' already exists; refusing to reuse or drop it." >&2
        echo "test-odoo: if this is your --keep run, re-run with OTI_TEST_RUN_ID=$project --resume." >&2
        exit 1
    fi
    echo "test-odoo: --resume: reusing existing database $database (not dropped; its volume is)"
fi

# ---------------------------------------------------------------------------
# 2. Install with tests. `--test-enable` implies --stop-after-init; both are
# passed so the intent reads off the command line. Odoo's exit status is the
# run's: preload_registries() returns the number of databases whose assertion
# report failed and cli/server.py exits with it, so a failing test is non-zero.
#
# A fresh run installs (`--init`). A --resume run has the module installed
# already, and `--init` on an installed module is a no-op — its tests are only
# collected for modules the load actually touched, so the run would report
# "0 tests" and prove nothing. `--update` re-loads it, which is what re-runs
# the tests against the kept database.
# ---------------------------------------------------------------------------
if [ "$resume" = 1 ]; then
    install_flag="--update=$module"
else
    install_flag="--init=$module"
fi
set +e
compose run --rm --no-deps \
    odoo \
    odoo server \
        --addons-path=/mnt/extra-addons \
        --data-dir=/var/lib/odoo \
        --database="$database" \
        "$install_flag" \
        --test-tags="${tags:-/$module}" \
        --test-enable \
        --stop-after-init \
        --log-level=info \
    2>&1 | tee "$log_file"
rc=${PIPESTATUS[0]}
set -e

# ---------------------------------------------------------------------------
# 3. Read the assertion report out of the log.
#
# Odoo prints one line per database, e.g.
#   "0 failed, 0 error(s) of 7 tests when loading database 'oti_test_…'"
# Without it, the two states this script has to tell apart — "all tests passed"
# and "no test was collected at all" — look identical from the exit code, since
# both are successful. That distinction is the difference between a green run
# and a run that proved nothing.
# ---------------------------------------------------------------------------
summary=$(grep -E '[0-9]+ failed, [0-9]+ error\(s\) of [0-9]+ tests' "$log_file" | tail -1 || true)
tests_run=$(printf '%s' "$summary" | sed -n 's/.*of \([0-9][0-9]*\) tests.*/\1/p')

if [ "$rc" -ne 0 ]; then
    echo
    echo "test-odoo: FAILED (odoo exit $rc)"
    [ -n "$summary" ] && echo "test-odoo: $summary"
    echo "test-odoo: log: $log_file"
    exit "$rc"
fi

echo
if [ -n "$summary" ]; then
    echo "test-odoo: $summary"
fi

if [ -z "$summary" ]; then
    # No assertion report at all: nothing ran, or the module did not load.
    echo "test-odoo: FAILED: Odoo reported no test result; the module may not have installed." >&2
    echo "test-odoo: log: $log_file" >&2
    exit 1
fi

echo "test-odoo: log: $log_file"

if [ "$tests_run" != "0" ]; then
    echo 'test-odoo: OK'
    exit 0
fi

# Zero tests. Two different situations, deliberately reported differently:
#
#   * the caller named a filter that matched nothing — they asked for specific
#     tests and none ran, so the run proved nothing they wanted: fail;
#   * no filter was named and the module simply has no tests yet — say so
#     loudly and succeed, because "installs cleanly" is still a real result and
#     OTI-5737 is the ticket that adds the first test. OTI_TEST_REQUIRE_TESTS=1
#     makes CI treat it as the failure it would become.
if [ -n "$tags" ]; then
    echo "test-odoo: FAILED: --test-tags '$tags' matched no test." >&2
    echo "test-odoo: log: $log_file" >&2
    exit 1
fi

if [ "${OTI_TEST_REQUIRE_TESTS:-0}" = 1 ]; then
    echo "test-odoo: FAILED: no test was collected (OTI_TEST_REQUIRE_TESTS=1)." >&2
    echo "test-odoo: log: $log_file" >&2
    exit 1
fi

echo 'test-odoo: ############################################################'
echo 'test-odoo: # module installed, but NO TEST RAN — nothing was verified.  #'
echo 'test-odoo: # (no tests/ package yet; OTI_TEST_REQUIRE_TESTS=1 fails this)  #'
echo 'test-odoo: ############################################################'
exit 0
