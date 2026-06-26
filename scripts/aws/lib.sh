# Shared helpers for the scripts/aws/* migration scripts. Source after config.env.
set -euo pipefail

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# require_var VAR_NAME — fail early if a config value is still blank.
require_var() {
  local name="$1"
  local val="${!name:-}"
  [ -n "$val" ] || die "config.env: \$$name is empty — fill it in (see 01_discover.sh)."
}

# load config from the dir this lib lives in, regardless of CWD.
_AWS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${_AWS_DIR}/config.env"
