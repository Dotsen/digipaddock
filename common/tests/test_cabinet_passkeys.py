import json

import pytest

from kd.cabinet import config, passkeys
from softauth import SoftAuthenticator

CFG = config.load("/none.json", "/none.env", dev=True)


def test_register_then_login_discoverably_and_step_up_with_a_bound_challenge():
    auth = SoftAuthenticator(CFG.rp_id, CFG.origin)
    options, challenge = passkeys.registration_options(
        CFG, user_id="owner", user_name="Владелец", exclude=[]
    )
    parsed = json.loads(options)
    assert parsed["authenticatorSelection"]["residentKey"] == "required"
    assert parsed["authenticatorSelection"]["userVerification"] == "required"
    assert {p["alg"] for p in parsed["pubKeyCredParams"]} == {-7, -257}
    reg = passkeys.verify_registration(CFG, auth.register(options), challenge)
    assert reg.sign_count == 0 and reg.credential_id == auth.credential_id
    assert reg.transports == ["internal"]
    options, challenge = passkeys.authentication_options(CFG)
    assert (
        "allowCredentials" not in json.loads(options)
        or not json.loads(options)["allowCredentials"]
    )
    assertion = auth.assert_(options)
    assert passkeys.credential_id_of(assertion) == reg.credential_id
    count = passkeys.verify_authentication(
        CFG, assertion, challenge=challenge, public_key=reg.public_key, sign_count=0
    )
    assert count == 1
    bound = b"\x01" * 32
    options, challenge = passkeys.authentication_options(
        CFG, challenge=bound, allow=[reg.credential_id]
    )
    assert challenge == bound
    assert (
        passkeys.verify_authentication(
            CFG,
            auth.assert_(options),
            challenge=bound,
            public_key=reg.public_key,
            sign_count=1,
        )
        == 2
    )


def test_wrong_origin_challenge_counter_or_missing_uv_are_refused():
    auth = SoftAuthenticator(CFG.rp_id, CFG.origin)
    options, challenge = passkeys.registration_options(
        CFG, user_id="owner", user_name="Владелец", exclude=[]
    )
    reg = passkeys.verify_registration(CFG, auth.register(options), challenge)
    options, challenge = passkeys.authentication_options(CFG)
    good = auth.assert_(options)
    with pytest.raises(passkeys.PasskeyError):
        passkeys.verify_authentication(
            CFG, good, challenge=b"\x02" * 32, public_key=reg.public_key, sign_count=0
        )
    with pytest.raises(passkeys.PasskeyError):  # replayed counter
        passkeys.verify_authentication(
            CFG, good, challenge=challenge, public_key=reg.public_key, sign_count=5
        )
    evil = SoftAuthenticator(CFG.rp_id, "https://evil.example")
    options, challenge = passkeys.registration_options(
        CFG, user_id="x", user_name="x", exclude=[]
    )
    with pytest.raises(passkeys.PasskeyError):
        passkeys.verify_registration(CFG, evil.register(options), challenge)
    options, challenge = passkeys.authentication_options(CFG)
    with pytest.raises(passkeys.PasskeyError):
        passkeys.verify_authentication(
            CFG,
            auth.assert_(options, uv=False),
            challenge=challenge,
            public_key=reg.public_key,
            sign_count=1,
        )


def test_garbage_is_a_passkey_error():
    with pytest.raises(passkeys.PasskeyError):
        passkeys.credential_id_of("not json")
    with pytest.raises(passkeys.PasskeyError):
        passkeys.verify_registration(CFG, '{"id": 1}', b"x" * 32)
    with pytest.raises(passkeys.PasskeyError):
        passkeys.verify_authentication(
            CFG, "{}", challenge=b"x" * 32, public_key=b"", sign_count=0
        )


def test_the_user_handle_of_a_registered_passkey_never_changes():
    # The browser keeps the handle with the credential since the first registration: a new value orphans every key
    # (the product rename keeps the old salt).
    assert passkeys.user_handle("owner").hex() == "10f4c92c827454d7c646d1ea8742fb67"
