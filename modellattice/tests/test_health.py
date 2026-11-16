def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_empty_catalog(client):
    assert client.get("/v1/models").json() == []
    assert client.get("/v1/offerings").json() == []
