import inspect
import internetarchive as ia
import archive_storage


def test_real_internetarchive_contract():
    # Verify ArchiveSession and get_item signature in internetarchive 5.12.0
    assert hasattr(ia, "ArchiveSession")
    assert hasattr(ia, "get_item")
    assert hasattr(ia, "upload")
    assert hasattr(ia, "delete")

    session = ia.ArchiveSession(config={"s3": {"access": "acc", "secret": "sec"}})
    item = session.get_item("test-identifier")
    assert hasattr(item, "modify_metadata")

    get_item_sig = inspect.signature(ia.get_item)
    assert "identifier" in get_item_sig.parameters

    upload_sig = inspect.signature(ia.upload)
    assert "identifier" in upload_sig.parameters
    assert "files" in upload_sig.parameters
