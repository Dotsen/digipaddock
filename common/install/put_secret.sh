#!/usr/bin/env bash
# From the owner's machine: a secret from stdin → a file on the server (atomically, with an owner and permissions).
# usage: bash common/install/put_secret.sh <ssh-alias> <path> <owner:group> <mode> < tmp/secrets/<file>
# The secret's value gets neither into argv nor into the output.
# The directory belongs to root — root writes (chown on the target). The directory belongs to a user (agent, say) —
# the whole write goes on its behalf through runuser: root does no chown/chmod by a name that user could swap for a symlink.
set -euo pipefail
[ $# -eq 4 ] || { echo "usage: put_secret.sh <ssh-alias> <path> <owner:group> <mode>" >&2; exit 2; }
host=$1 path=$2 owner=$3 mode=$4
[[ $path =~ ^/[A-Za-z0-9._/-]+$ ]] && [[ $path != *..* ]] || { echo "invalid path: $path" >&2; exit 2; }
[[ $path != */ && $path != */. ]] || { echo "the path must name a file, not a directory: $path" >&2; exit 2; }
# on the server the path is compared with getcwd() — "//" and "/./" would give a false refusal "there is a symlink"
[[ $path != *//* && $path != */./* ]] || { echo "non-canonical path (// or /./): $path" >&2; exit 2; }
[[ $owner =~ ^[a-z_][a-z0-9_-]*:[a-z_][a-z0-9_-]*$ ]] || { echo "invalid owner:group: $owner" >&2; exit 2; }
[[ $mode =~ ^[0-7]{3,4}$ ]] || { echo "invalid mode: $mode" >&2; exit 2; }

# the body of the remote part is the shared writer secret_write.sh (the same one kd-secret put uses on the host itself)
remote=$(cat "$(dirname "${BASH_SOURCE[0]}")/secret_write.sh")
# the script — through base64 (stdin is taken by the secret); the arguments are checked above, they have no quotes or spaces
b64=$(printf '%s\n' "$remote" | base64 | tr -d '\n')
ssh "$host" "bash -c \"\$(echo $b64 | base64 -d)\" _ $path $owner $mode"
