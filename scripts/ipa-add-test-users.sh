#!/bin/sh
# Create a handful of FreeIPA test users + groups for dev workflows.
#
# Runs `ipa` commands inside the freeipa-server container, so the host
# never needs the FreeIPA client tools. Pass IPA_ADMIN_PASS in the
# environment.
#
# Users created (all with password "ChangeMe123!" for dev):
#   alice    — student     in group `students`
#   bob      — student     in group `students`
#   carol    — teacher     in group `teachers`
#   dave     — headmaster  in group `headmaster`
#
# Idempotent: re-running won't fail on existing entries — `ipa user-add`
# returns "already exists" which we catch.

set -eu

CONTAINER="${IPA_CONTAINER:-freeipa-server}"
INITIAL_PASS="${IPA_TEST_PASSWORD:-ChangeMe123!}"

if [ -z "${IPA_ADMIN_PASS:-}" ]; then
    echo "error: IPA_ADMIN_PASS must be set" >&2
    exit 2
fi

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "error: container '$CONTAINER' not found (run setup-ipa-server.sh first)" >&2
    exit 1
fi

ipa_in_container() {
    # ipa command line tools need a Kerberos ticket; we kinit with the
    # admin password each time. The container already has a /etc/krb5.conf
    # pointing at itself.
    docker exec -i "$CONTAINER" sh -c "
        echo \"$IPA_ADMIN_PASS\" | kinit admin >/dev/null 2>&1
        $*
    "
}

# --- groups -----------------------------------------------------------------
for group in students teachers headmaster; do
    if ipa_in_container "ipa group-show $group" >/dev/null 2>&1; then
        echo "group $group exists"
    else
        echo "adding group $group"
        ipa_in_container "ipa group-add $group --desc='$group group'"
    fi
done

# --- users ------------------------------------------------------------------
add_user() {
    login="$1"
    first="$2"
    last="$3"
    group="$4"

    if ipa_in_container "ipa user-show $login" >/dev/null 2>&1; then
        echo "user $login exists; resetting password"
    else
        echo "adding user $login"
        ipa_in_container "ipa user-add $login --first='$first' --last='$last' --password" <<EOF
$INITIAL_PASS
$INITIAL_PASS
EOF
    fi
    # Make sure the user is in the right group.
    ipa_in_container "ipa group-add-member $group --users=$login" \
        || true
}

add_user alice  Alice  Test  students
add_user bob    Bob    Test  students
add_user carol  Carol  Test  teachers
add_user dave   Dave   Test  headmaster

cat <<EOF

=> done. Test login credentials (dev only):
   alice / $INITIAL_PASS
   bob   / $INITIAL_PASS
   carol / $INITIAL_PASS
   dave  / $INITIAL_PASS

   All accounts must change their password on first login (FreeIPA default).
EOF
