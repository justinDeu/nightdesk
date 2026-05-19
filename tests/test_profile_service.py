import pytest

from nightdesk.domain.profiles import (
    create_profile, get_profile, list_profiles, update_profile, delete_profile,
    ProfileNotFound, ProfileNameTaken,
)


def test_create_profile_persists(session):
    p = create_profile(session, name="repo-writer", fs_read=["/r"], fs_write=["/r"],
                        allowed_tools=["Read"], denied_tools=[], network_mode="off",
                        network_allowlist=[], secret_keys=[], default_model=None)
    assert p.id
    fetched = get_profile(session, p.id)
    assert fetched.name == "repo-writer"


def test_create_profile_unique_name(session):
    create_profile(session, name="x", fs_read=[], fs_write=[], allowed_tools=[],
                    denied_tools=[], network_mode="off", network_allowlist=[], secret_keys=[],
                    default_model=None)
    with pytest.raises(ProfileNameTaken):
        create_profile(session, name="x", fs_read=[], fs_write=[], allowed_tools=[],
                        denied_tools=[], network_mode="off", network_allowlist=[],
                        secret_keys=[], default_model=None)


def test_list_profiles(session, sample_profile):
    ps = list_profiles(session)
    assert len(ps) == 1


def test_update_profile(session, sample_profile):
    updated = update_profile(session, sample_profile.id, network_mode="open")
    assert updated.network_mode == "open"


def test_delete_profile(session, sample_profile):
    delete_profile(session, sample_profile.id)
    with pytest.raises(ProfileNotFound):
        get_profile(session, sample_profile.id)
