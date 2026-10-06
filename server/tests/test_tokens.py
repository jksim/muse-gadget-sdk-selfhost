import hashlib

import pytest

from musehost.store import Store
from musehost.tokens import ACCESS_TTL_S, GRANT_TTL_S, VM_TTL_S, Tokens


class Clock:
    def __init__(self, now: int = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def tokens(tmp_path, clock):
    return Tokens(Store.open(tmp_path / "musehost.db"), clock=clock)


def test_an_enrolled_device_is_found_by_its_access_token(tokens):
    access, _ = tokens.enroll("homelink-abcdef", "pi")
    assert tokens.device_for_access(access) == "homelink-abcdef"


def test_an_access_token_expires_after_four_hours(tokens, clock):
    access, _ = tokens.enroll("homelink-abcdef", "pi")
    clock.now += ACCESS_TTL_S
    assert tokens.device_for_access(access) is None


def test_unknown_and_refresh_tokens_are_not_access_tokens(tokens):
    _, refresh = tokens.enroll("homelink-abcdef", "pi")
    assert tokens.device_for_access("nope") is None
    assert tokens.device_for_access(refresh) is None


def test_tokens_are_stored_only_as_hashes(tokens):
    access, refresh = tokens.enroll("homelink-abcdef", "pi")
    stored = {row[0] for row in tokens.store.db.execute("SELECT hash FROM tokens")}
    assert access not in stored and refresh not in stored
    assert hashlib.sha256(access.encode()).hexdigest() in stored


@pytest.mark.parametrize("node_id", ["homelink-ABCDEF", "homelink-abcde", "pi", "homelink-abcdefg"])
def test_a_malformed_node_id_cannot_enroll(tokens, node_id):
    with pytest.raises(ValueError):
        tokens.enroll(node_id, "pi")


def test_enrolling_again_replaces_the_old_tokens(tokens):
    old_access, _ = tokens.enroll("homelink-abcdef", "pi")
    new_access, _ = tokens.enroll("homelink-abcdef", "pi")
    assert tokens.device_for_access(old_access) is None
    assert tokens.device_for_access(new_access) == "homelink-abcdef"


def test_a_vm_token_is_valid_for_its_vm_for_fifteen_minutes(tokens, clock):
    tokens.enroll("homelink-abcdef", "pi")
    vm_token = tokens.issue_vm_token("homelink-abcdef", "home")
    assert tokens.verify_vm_token(vm_token, "home") == "homelink-abcdef"
    assert tokens.verify_vm_token(vm_token, "den") is None
    clock.now += VM_TTL_S
    assert tokens.verify_vm_token(vm_token, "home") is None


def test_an_access_token_is_not_a_vm_token(tokens):
    access, _ = tokens.enroll("homelink-abcdef", "pi")
    assert tokens.verify_vm_token(access, "home") is None


def test_a_revoked_device_has_no_valid_tokens(tokens):
    access, _ = tokens.enroll("homelink-abcdef", "pi")
    vm_token = tokens.issue_vm_token("homelink-abcdef", "home")
    tokens.revoke("homelink-abcdef")
    assert tokens.device_for_access(access) is None
    assert tokens.verify_vm_token(vm_token, "home") is None


def test_using_an_access_token_records_when_the_device_was_last_seen(tokens, clock):
    access, _ = tokens.enroll("homelink-abcdef", "pi")
    clock.now += 60
    tokens.device_for_access(access)
    row = tokens.store.db.execute(
        "SELECT last_seen FROM devices WHERE node_id = 'homelink-abcdef'"
    ).fetchone()
    assert row[0] == clock.now


# -- Refresh rotation ------------------------------------------------------------


def test_refresh_rotates_both_tokens(tokens):
    access, refresh = tokens.enroll("homelink-abcdef", "pi")
    new_access, new_refresh = tokens.refresh(refresh, "homelink-abcdef")
    assert {new_access, new_refresh}.isdisjoint({access, refresh})
    assert tokens.device_for_access(new_access) == "homelink-abcdef"


def test_the_old_access_token_works_until_the_new_pair_is_used(tokens):
    access, refresh = tokens.enroll("homelink-abcdef", "pi")
    new_access, _ = tokens.refresh(refresh, "homelink-abcdef")
    assert tokens.device_for_access(access) == "homelink-abcdef"
    tokens.device_for_access(new_access)
    assert tokens.device_for_access(access) is None


def test_a_lost_response_retry_gets_a_working_replacement_pair(tokens):
    _, refresh = tokens.enroll("homelink-abcdef", "pi")
    lost_access, _ = tokens.refresh(refresh, "homelink-abcdef")
    retry_access, retry_refresh = tokens.refresh(refresh, "homelink-abcdef")
    assert tokens.device_for_access(retry_access) == "homelink-abcdef"
    assert tokens.device_for_access(lost_access) is None
    assert tokens.refresh(retry_refresh, "homelink-abcdef") is not None


def test_a_lost_response_retry_works_hours_later_while_the_pair_is_unused(tokens, clock):
    old_access, refresh = tokens.enroll("homelink-abcdef", "pi")
    tokens.refresh(refresh, "homelink-abcdef")  # response lost
    clock.now += 3 * 3600  # a long Noise session on the old tokens
    retry_access, _ = tokens.refresh(refresh, "homelink-abcdef")
    assert tokens.device_for_access(retry_access) == "homelink-abcdef"


def test_repeated_lost_responses_keep_retrying(tokens):
    _, refresh = tokens.enroll("homelink-abcdef", "pi")
    tokens.refresh(refresh, "homelink-abcdef")
    tokens.refresh(refresh, "homelink-abcdef")
    last_access, _ = tokens.refresh(refresh, "homelink-abcdef")
    assert tokens.device_for_access(last_access) == "homelink-abcdef"


def test_presenting_a_superseded_refresh_token_revokes(tokens):
    _, refresh = tokens.enroll("homelink-abcdef", "pi")
    _, superseded = tokens.refresh(refresh, "homelink-abcdef")
    retry_access, _ = tokens.refresh(refresh, "homelink-abcdef")
    assert tokens.refresh(superseded, "homelink-abcdef") is None
    assert tokens.device_for_access(retry_access) is None


def test_an_attacker_replaying_the_previous_refresh_token_ends_in_revocation(tokens):
    _, stolen = tokens.enroll("homelink-abcdef", "pi")
    legit_access, legit_refresh = tokens.refresh(stolen, "homelink-abcdef")
    attacker_access, _ = tokens.refresh(stolen, "homelink-abcdef")
    # The gadget's pair is now superseded: its access token is refused...
    assert tokens.device_for_access(legit_access) is None
    # ...so it refreshes, which exposes the replay and revokes everything.
    assert tokens.refresh(legit_refresh, "homelink-abcdef") is None
    assert tokens.device_for_access(attacker_access) is None


def test_reusing_an_old_refresh_token_after_the_new_access_token_was_used_revokes(tokens):
    _, refresh = tokens.enroll("homelink-abcdef", "pi")
    new_access, _ = tokens.refresh(refresh, "homelink-abcdef")
    tokens.device_for_access(new_access)
    assert tokens.refresh(refresh, "homelink-abcdef") is None
    assert tokens.device_for_access(new_access) is None


def test_reusing_an_old_refresh_token_after_the_new_one_rotated_revokes(tokens):
    _, refresh = tokens.enroll("homelink-abcdef", "pi")
    _, second = tokens.refresh(refresh, "homelink-abcdef")
    third_access, _ = tokens.refresh(second, "homelink-abcdef")
    assert tokens.refresh(refresh, "homelink-abcdef") is None
    assert tokens.device_for_access(third_access) is None


def test_a_refresh_for_another_device_id_fails_without_revoking(tokens):
    _, refresh = tokens.enroll("homelink-abcdef", "pi")
    assert tokens.refresh(refresh, "homelink-123456") is None
    assert tokens.refresh(refresh, "homelink-abcdef") is not None


def test_an_access_token_cannot_refresh(tokens):
    access, _ = tokens.enroll("homelink-abcdef", "pi")
    assert tokens.refresh(access, "homelink-abcdef") is None


def test_a_revoked_device_cannot_refresh(tokens):
    _, refresh = tokens.enroll("homelink-abcdef", "pi")
    tokens.revoke("homelink-abcdef")
    assert tokens.refresh(refresh, "homelink-abcdef") is None


# -- Enrollment grants -----------------------------------------------------------


def test_a_grant_works_once(tokens):
    code = tokens.create_grant()
    assert tokens.redeem_grant(code)
    assert not tokens.redeem_grant(code)


def test_a_grant_expires_after_ten_minutes(tokens, clock):
    code = tokens.create_grant()
    clock.now += GRANT_TTL_S
    assert not tokens.redeem_grant(code)


def test_an_unknown_grant_is_refused(tokens):
    assert not tokens.redeem_grant("nope")
    assert not tokens.redeem_grant("")


def test_grants_carry_128_bits_and_are_stored_hashed(tokens):
    code = tokens.create_grant()
    assert len(code) >= 22  # 16 random bytes, base64url
    stored = {row[0] for row in tokens.store.db.execute("SELECT hash FROM grants")}
    assert code not in stored and hashlib.sha256(code.encode()).hexdigest() in stored


# -- Housekeeping ----------------------------------------------------------------


def count(tokens, kind: str) -> int:
    return tokens.store.db.execute(
        "SELECT count(*) FROM tokens WHERE kind = ?", (kind,)
    ).fetchone()[0]


def test_rotated_refresh_tombstones_are_kept_so_late_reuse_still_revokes(tokens, clock):
    _, first = tokens.enroll("homelink-abcdef", "pi")
    access, second = tokens.refresh(first, "homelink-abcdef")
    tokens.device_for_access(access)
    for _ in range(3):
        clock.now += 40 * 24 * 3600
        access, second = tokens.refresh(second, "homelink-abcdef")
        tokens.device_for_access(access)
    assert tokens.refresh(first, "homelink-abcdef") is None
    assert tokens.device_for_access(access) is None


def test_expired_access_tokens_are_pruned(tokens, clock):
    access, refresh = tokens.enroll("homelink-abcdef", "pi")
    for _ in range(5):
        clock.now += ACCESS_TTL_S
        access, refresh = tokens.refresh(refresh, "homelink-abcdef")
    assert count(tokens, "access") == 1
