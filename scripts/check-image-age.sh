#!/usr/bin/env bash
# Pins must be at least two weeks old (docs/decisions.md S2): fail if an image in monitoring/compose.yaml was built
# less than MIN_DAYS ago. Dependabot's cooldown does not hold for every registry, so CI checks it on pull requests.
# Needs network. Uses skopeo if installed (it is on GitHub's Ubuntu runners), else its container image.
#
#   scripts/check-image-age.sh [MIN_DAYS]        # `make image-age`
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
MIN_DAYS=${1:-14}
inspect() {
  if command -v skopeo >/dev/null; then skopeo inspect --override-os linux --override-arch arm64 "$@"
  else docker run --rm quay.io/skopeo/stable:v1.22.3 inspect --override-os linux --override-arch arm64 "$@"; fi
}
rc=0
while read -r image; do
  created=$(inspect --format '{{.Created.Format "2006-01-02T15:04:05Z"}}' "docker://$image" 2>/dev/null)
  days=$(python3 -I -c 'import datetime as d, sys
t = d.datetime.strptime(sys.argv[1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=d.timezone.utc)
print((d.datetime.now(d.timezone.utc) - t).days)' "$created" 2>/dev/null)
  if [ -z "$days" ]; then printf '  \033[31m✗\033[0m %s: could not read the build date\n' "$image"; rc=1
  elif [ "$days" -lt "$MIN_DAYS" ]; then
    printf '  \033[31m✗\033[0m %s: built %s days ago, pins must be at least %s days old (S2)\n' "$image" "$days" "$MIN_DAYS"; rc=1
  else printf '  \033[32m✓\033[0m %s: built %s days ago\n' "$image" "$days"; fi
done < <(sed -n 's/^ *image: *\([^ #]*\).*$/\1/p' monitoring/compose.yaml)
exit $rc
