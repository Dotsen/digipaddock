#!/usr/bin/env bash
# A secret from stdin → the file <path> (atomically, with an owner and permissions), as root on the host itself.
# usage: secret_write.sh <path> <owner:group> <mode> < value
# Called by put_secret.sh (from the owner's machine, over ssh) and kd-secret put (on the host); they check the arguments.
# The directory belongs to root — root writes (chown on the target). The directory belongs to a user (agent, say) —
# the whole write goes on its behalf through runuser: root does no chown/chmod by a name that user could swap for a symlink.
set -e
path=$1 owner=$2 mode=$3
dir=$(dirname "$path")
file=${path##*/}
[ -d "$dir" ] || { echo "no directory $dir" >&2; exit 1; }
# pin the directory: from here both the owner check and the write go by "." (not by the path string, where agent could
# swap a component for a symlink)
cd -P -- "$dir"
# a symlink in the path (agent could have planted it) would lead into a foreign directory. The real path of the pinned
# inode comes only from getcwd() — /bin/pwd (coreutils) calls it. bash's builtin pwd will not do: it parses the path
# string again, and agent can swap a component between the parses (renameat2 RENAME_EXCHANGE). The path is taken once.
real=$(/bin/pwd -P)
[ "$real" = "$dir" ] || { echo "the path $dir has a symlink (→ $real) — refused" >&2; exit 1; }
downer=$(stat -c %U .)
if [ "$downer" = root ]; then
  perm=$(printf %03d "$(stat -c %a .)")  # режим 0-7 -> 000-007, иначе 8# падает
  if [ $(( (8#${perm: -2:1} | 8#${perm: -1}) & 2 )) -ne 0 ]; then
    echo "directory $dir is writable by group or others ($perm) — refused" >&2; exit 1
  fi
  umask 077
  tmp=$(mktemp ./.secret.XXXXXX)
  trap 'rm -f "$tmp"' EXIT
  cat > "$tmp"
  chown "$owner" "$tmp"
  chmod "$mode" "$tmp"
  mv -fT "$tmp" "./$file"
else
  [ "${owner%%:*}" = "$downer" ] || { echo "directory $dir belongs to $downer, but owner ${owner%%:*} was requested" >&2; exit 1; }
  dgroup=$(id -gn "$downer")
  [ "${owner##*:}" = "$dgroup" ] || { echo "the file group is the primary group of $downer ($dgroup), but ${owner##*:} was requested" >&2; exit 1; }
  # runuser inherits the pinned cwd
  runuser -u "$downer" -- sh -c 'set -e; umask 077
    tmp=$(mktemp ./.secret.XXXXXX)
    trap "rm -f \"\$tmp\"" EXIT
    cat > "$tmp"
    chmod "$2" "$tmp"
    mv -fT "$tmp" "./$1"' _ "$file" "$mode"
fi
echo "written $real/$file ($(stat -c '%U:%G %a' "./$file"))"
