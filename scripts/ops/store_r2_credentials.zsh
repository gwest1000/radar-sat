#!/bin/zsh

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]] || ! command -v security >/dev/null 2>&1; then
  print -u2 "This helper requires the macOS security command."
  exit 2
fi

account="radar-sat"
account_id="${RADARSAT_R2_ACCOUNT_ID:-52ff7b6afb8d56ad477b6d61ff96157e}"
bucket="${RADARSAT_R2_BUCKET:-radar-sat}"
public_url="${RADARSAT_R2_PUBLIC_BASE_URL:-https://pub-2cb8162da84b4c1eb0e445844f2a89a3.r2.dev}"

read "access_key?New radar-sat R2 access key ID: "
read -s "secret_key?New radar-sat R2 secret access key: "
print

if [[ -z "${access_key}" || -z "${secret_key}" ]]; then
  print -u2 "Both credential fields are required; Keychain was not changed."
  exit 2
fi

store() {
  security add-generic-password -U -a "${account}" -s "$1" -w "$2" >/dev/null
}

store radar-sat-r2-account-id "${account_id}"
store radar-sat-r2-access-key-id "${access_key}"
store radar-sat-r2-secret-access-key "${secret_key}"
store radar-sat-r2-bucket "${bucket}"
store radar-sat-r2-public-base-url "${public_url}"

unset access_key secret_key
print "Stored the five Radar-Sat R2 fields in macOS Keychain for account ${account}."
print "Next: PYTHONPATH=. .venv/bin/python scripts/publish_r2.py --root data/output --dry-run"
