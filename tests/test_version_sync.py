from scripts.version import read_versions


def test_release_versions_match_root_version() -> None:
    versions = read_versions()
    assert len(set(versions.values())) == 1, versions
