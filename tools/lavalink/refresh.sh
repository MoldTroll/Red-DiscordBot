#!/bin/bash
# Installed root-owned alongside release.py. All JAR and config access is unprivileged.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unit=${1:?Lavalink service required}
shift
(( $# )) || { echo "Specify the RedBot service units that use Lavalink." >&2; exit 1; }
bots=("$@")
owner=$(systemctl show -p User --value "$unit")
dir=$(systemctl show -p WorkingDirectory --value "$unit")
[[ -n "$owner" && $(id -u "$owner") != 0 && "$dir" == /* ]]
helper="$(dirname "$(realpath "$0")")/release.py"
as_owner() { runuser -u "$owner" -- /usr/bin/python3 -I "$helper" "$1" "$dir"; }

exec 9>/run/lock/lavalink-refresh.lock
flock -n 9 || exit 0
port=$(as_owner port)
[[ "$port" =~ ^[0-9]+$ ]]
connected() {
    local peers bot pid
    peers=$(ss -Htnp state established "( dport = :$port )") || return 1
    for bot in "${bots[@]}"; do
        systemctl is-active --quiet "$bot" || return 1
        pid=$(systemctl show -p MainPID --value "$bot") || return 1
        [[ "$pid" != 0 && "$peers" == *"pid=$pid,"* ]] || return 1
    done
}
restart_and_check() {
    systemctl restart "$unit" || return 1
    for ((i=0; i<60; i++)); do
        if connected; then as_owner check; return $?; fi
        sleep 2
    done
    return 1
}
# Repair interrupted transactions even when the failed JAR prevents clients connecting.
status=0
runuser -u "$owner" -- test -f "$dir/.lavalink-refresh/pending" && status=12
if ((status == 0)); then
    connected || { echo "Deferring update while the RedBot clients are disconnected."; exit 0; }
    as_owner update || status=$?
fi
case "$status" in
    10) exit 0 ;;
    0) if restart_and_check; then as_owner commit; exit 0; fi ;;
    12) echo "Recovering an interrupted update." ;;
    *) exit "$status" ;;
esac
echo "Restoring the previous Lavalink JAR." >&2
as_owner rollback
restart_and_check
as_owner commit
exit 1
