#!/usr/bin/env bash
# Apply the GitHub repository settings this project expects. Idempotent; needs `gh` logged in as a repo admin.
#
#   scripts/github-settings.sh            # settings that work on any repo
#   scripts/github-settings.sh --public   # also the ones GitHub offers only on public repos (free plan):
#                                         # secret scanning + push protection, private vulnerability reporting,
#                                         # CodeQL default setup, and a ruleset protecting the default branch
set -euo pipefail
REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)
api() { gh api -H "Accept: application/vnd.github+json" "$@" >/dev/null; }
say() { printf '  \033[32m✓\033[0m %s\n' "$*"; }

echo "Settings for $REPO"
api -X PATCH "repos/$REPO" -F has_wiki=false -F has_projects=false -F delete_branch_on_merge=true \
  -F allow_merge_commit=false -F allow_squash_merge=true -F allow_rebase_merge=true -F allow_auto_merge=true
say "wiki and projects off, squash or rebase merges only, merged branches deleted"
api -X PUT "repos/$REPO/vulnerability-alerts"
api -X PUT "repos/$REPO/automated-security-fixes"
say "Dependabot alerts and security updates on"
api -X PUT "repos/$REPO/actions/permissions" -F enabled=true -f allowed_actions=all -F sha_pinning_required=true
api -X PUT "repos/$REPO/actions/permissions/workflow" -f default_workflow_permissions=read \
  -F can_approve_pull_request_reviews=false
say "Actions must be pinned to a commit SHA; workflow token read-only by default"

[ "${1:-}" = --public ] || { echo "Run again with --public once the repository is public."; exit 0; }

api -X PATCH "repos/$REPO" --input - <<'JSON'
{"security_and_analysis": {"secret_scanning": {"status": "enabled"},
                           "secret_scanning_push_protection": {"status": "enabled"}}}
JSON
say "secret scanning and push protection on"
api -X PUT "repos/$REPO/private-vulnerability-reporting"
say "private vulnerability reporting on (SECURITY.md links to it)"
api -X PATCH "repos/$REPO/code-scanning/default-setup" --input - <<'JSON'
{"state": "configured", "query_suite": "default", "languages": ["python", "actions"]}
JSON
say "CodeQL default setup on for Python and Actions workflows"

# Default branch: no deletion, no force-push, changes arrive by pull request with the lint check green.
# The repository admin may bypass, so a sole maintainer is not locked out.
RULESET=$(cat <<'JSON'
{"name": "main", "target": "branch", "enforcement": "active",
 "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
 "bypass_actors": [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}],
 "rules": [
   {"type": "deletion"},
   {"type": "non_fast_forward"},
   {"type": "pull_request", "parameters": {"required_approving_review_count": 0,
     "dismiss_stale_reviews_on_push": true, "require_code_owner_review": false,
     "require_last_push_approval": false, "required_review_thread_resolution": true}},
   {"type": "required_status_checks", "parameters": {"strict_required_status_checks_policy": false,
     "required_status_checks": [{"context": "lint"}]}}
 ]}
JSON
)
id=$(gh api "repos/$REPO/rulesets" -q '.[] | select(.name == "main") | .id')
if [ -n "$id" ]; then echo "$RULESET" | api -X PUT "repos/$REPO/rulesets/$id" --input -
else echo "$RULESET" | api -X POST "repos/$REPO/rulesets" --input -; fi
say "ruleset 'main': no deletion or force-push, pull request + lint check required (admin may bypass)"
