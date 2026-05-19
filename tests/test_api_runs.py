async def test_runs_empty_initially(client):
    r = await client.get("/api/v1/runs")
    assert r.status_code == 200
    assert r.json() == []
