def test_200_attributes(test_client):
    r = test_client.get("/data-source/attributes")

    assert r.status_code == 200
    assert len(r.json()) > 0


def test_200_metadata_attributes(test_client):
    r = test_client.get("/data-source/metadata-attributes")

    assert r.status_code == 200
    assert len(r.json()) > 0
