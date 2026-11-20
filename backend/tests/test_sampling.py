"""采集轮次编排、领用派发与停用并发的回归测试。

覆盖需求里的四个关键约束：

1. 按启用状态与周期编排轮次，地址与周期、截止时间在同一轮次成套生效；
2. 同一来源同一轮次只能派发一次（并发领取只能成功一个）；
3. 来源停用时，未派发轮次与未确认派发记录不能再派出；
4. 未领用的轮次收到新版本地址时，整轮按新版本成套刷新。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.sampling import sampling_service
from app.store import store


@pytest.fixture()
def client() -> TestClient:
    store.reset()
    return TestClient(app)


def _generate(client: TestClient, start: str, end: str, date: str = "2026-11-20") -> None:
    response = client.post(
        "/api/sampling/rounds",
        json={"values": {"start": start, "end": end, "date": date}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True, response.json()


def _items(client: TestClient, **params: object) -> list[dict]:
    response = client.get("/api/sampling/items", params={"size": 200, **params})
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _dispatches(client: TestClient, **params: object) -> list[dict]:
    response = client.get("/api/sampling/dispatches", params={"size": 200, **params})
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _enable(client: TestClient, entry_id: int) -> None:
    response = client.post(
        f"/api/datasource/{entry_id}/actions", json={"values": {"action": "启用配置"}}
    )
    assert response.json()["ok"] is True, response.json()


def _disable(client: TestClient, entry_id: int) -> None:
    response = client.post(
        f"/api/datasource/{entry_id}/actions", json={"values": {"action": "停用配置"}}
    )
    assert response.json()["ok"] is True, response.json()


def _change_address(client: TestClient, entry_id: int, address: str, period: str = "10分钟") -> None:
    response = client.put(
        f"/api/datasource/{entry_id}",
        json={"values": {"接入地址": address, "采样周期": period}},
    )
    assert response.json()["ok"] is True, response.json()


def test_rounds_follow_enabled_sources_and_period(client: TestClient) -> None:
    # 只有 DATA-0003 已启用；草稿/待校验来源不生成轮次。
    _generate(client, "12:00", "12:30")
    items = _items(client)
    assert {item["来源编号"] for item in items} == {"DATA-0003"}
    assert [item["计划分钟"] for item in items] == ["12:00", "12:10", "12:20", "12:30"]

    # 每个轮次的接入地址、周期、截止时间来自同一条来源版本。
    for item in items:
        assert item["来源版本"] == 1
        assert item["接入地址"] == "https://sample3.example.com/api/records"
        assert item["采样周期分钟"] == 10
    by_minute = {item["计划分钟"]: item for item in items}
    assert by_minute["12:20"]["截止分钟"] == "12:30"

    # 重复编排同一分钟不会产生第二条轮次。
    _generate(client, "12:20", "12:20")
    assert len(_items(client, round_key="2026-11-20T12:20")) == 1


def test_period_alignment_respects_source_cycle(client: TestClient) -> None:
    _enable(client, 1)
    _change_address(client, 1, "https://sample1.example.com/v2", period="15分钟")
    _generate(client, "12:00", "12:45")

    source_one = [item["计划分钟"] for item in _items(client, source_id=1)]
    assert source_one == ["12:00", "12:15", "12:30", "12:45"]
    source_three = [item["计划分钟"] for item in _items(client, source_id=3)]
    assert source_three == ["12:00", "12:10", "12:20", "12:30", "12:40"]


def test_same_round_dispatched_only_once_even_with_concurrent_claims(client: TestClient) -> None:
    _generate(client, "12:20", "12:20")
    item = _items(client, round_key="2026-11-20T12:20")[0]

    results: list[dict] = []
    barrier = threading.Barrier(2)

    def claim(operator: str) -> None:
        barrier.wait()
        response = client.post(
            f"/api/sampling/items/{item['id']}/claim",
            json={"values": {"operator": operator}},
        )
        results.append(response.json())

    threads = [threading.Thread(target=claim, args=(name,)) for name in ("操作员甲", "操作员乙")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(1 for result in results if result["ok"]) == 1
    assert sum(1 for result in results if not result["ok"]) == 1

    dispatches = _dispatches(client, round_key="2026-11-20T12:20", valid=True)
    assert len(dispatches) == 1
    dispatch = dispatches[0]
    assert dispatch["接入地址"] == "https://sample3.example.com/api/records"
    assert dispatch["截止分钟"] == "12:30"
    assert dispatch["来源版本"] == 1
    assert dispatch["status"] == "待确认"


def test_unclaimed_round_picks_up_new_address_as_a_set(client: TestClient) -> None:
    _generate(client, "12:20", "12:20")
    _change_address(client, 3, "https://sample3.example.com/api/v2", period="10分钟")

    item = _items(client, round_key="2026-11-20T12:20")[0]
    assert item["status"] == "待领用"
    assert item["接入地址"] == "https://sample3.example.com/api/v2"
    assert item["来源版本"] == 2
    # 地址、周期、截止分钟属于同一版本的一套安排。
    assert item["采样周期分钟"] == 10
    assert item["截止分钟"] == "12:30"

    response = client.post(f"/api/sampling/items/{item['id']}/claim", json={"values": {}})
    assert response.json()["ok"] is True
    dispatch = response.json()["entry"]
    assert dispatch["接入地址"] == "https://sample3.example.com/api/v2"
    assert dispatch["截止分钟"] == "12:30"
    assert dispatch["来源版本"] == 2


def test_disabled_source_cannot_dispatch_unconfirmed_records(client: TestClient) -> None:
    _generate(client, "12:20", "12:30")
    item = _items(client, round_key="2026-11-20T12:20")[0]

    claimed = client.post(f"/api/sampling/items/{item['id']}/claim", json={"values": {}}).json()
    assert claimed["ok"] is True
    dispatch = claimed["entry"]

    _disable(client, 3)

    # 尚未确认的记录被作废，不能再确认派发。
    confirm = client.post(f"/api/sampling/dispatches/{dispatch['id']}/confirm", json={"values": {}})
    assert confirm.json()["ok"] is False
    assert _dispatches(client, valid=True) == []

    # 同来源后续轮次也领用不了。
    later = _items(client, round_key="2026-11-20T12:30")[0]
    blocked = client.post(f"/api/sampling/items/{later['id']}/claim", json={"values": {}})
    assert blocked.json()["ok"] is False
    assert _items(client, round_key="2026-11-20T12:30")[0]["status"] == "已作废"


def test_disable_racing_claim_never_leaves_unconfirmed_dispatch(client: TestClient) -> None:
    _generate(client, "12:20", "12:20")
    item = _items(client, round_key="2026-11-20T12:20")[0]

    barrier = threading.Barrier(2)
    outcomes: dict[str, dict] = {}

    def claim() -> None:
        barrier.wait()
        outcomes["claim"] = client.post(
            f"/api/sampling/items/{item['id']}/claim", json={"values": {}}
        ).json()

    def disable() -> None:
        barrier.wait()
        outcomes["disable"] = client.post(
            "/api/datasource/3/actions", json={"values": {"action": "停用配置"}}
        ).json()

    threads = [threading.Thread(target=claim), threading.Thread(target=disable)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # 不管谁先拿到锁：停用之后都不允许留下有效的未确认派发记录。
    assert outcomes["disable"]["ok"] is True
    assert _dispatches(client, valid=True) == []
    assert all(row["status"] == "已作废" for row in _dispatches(client))


def test_confirmed_dispatch_keeps_its_snapshot(client: TestClient) -> None:
    _generate(client, "12:20", "12:20")
    item = _items(client, round_key="2026-11-20T12:20")[0]
    dispatch = client.post(f"/api/sampling/items/{item['id']}/claim", json={"values": {}}).json()["entry"]

    # 已领用但尚未确认时换地址：不能改动已领用轮次的快照。
    _change_address(client, 3, "https://sample3.example.com/api/v3")
    claimed_item = _items(client, round_key="2026-11-20T12:20")[0]
    assert claimed_item["接入地址"] == "https://sample3.example.com/api/records"
    assert claimed_item["截止分钟"] == "12:30"

    confirmed = client.post(f"/api/sampling/dispatches/{dispatch['id']}/confirm", json={"values": {}})
    assert confirmed.json()["ok"] is True
    entry = confirmed.json()["entry"]
    assert entry["接入地址"] == "https://sample3.example.com/api/records"
    assert entry["截止分钟"] == "12:30"
    assert entry["来源版本"] == 1

    # 已派发的轮次不会被刷新成新版本，也不能再次领用。
    again = client.post(f"/api/sampling/items/{item['id']}/claim", json={"values": {}})
    assert again.json()["ok"] is False
    assert len(_dispatches(client, round_key="2026-11-20T12:20", valid=True)) == 1


def test_midday_address_switch_keeps_one_valid_dispatch(client: TestClient) -> None:
    """午间切换地址：两名操作员领取 12:20 轮次，只能留下一条有效派发。"""
    _generate(client, "12:00", "12:30")
    _change_address(client, 3, "https://sample3.example.com/api/noon")

    target = _items(client, round_key="2026-11-20T12:20")[0]
    assert target["接入地址"] == "https://sample3.example.com/api/noon"

    barrier = threading.Barrier(2)
    results: list[dict] = []

    def claim(operator: str) -> None:
        barrier.wait()
        results.append(
            client.post(
                f"/api/sampling/items/{target['id']}/claim",
                json={"values": {"operator": operator}},
            ).json()
        )

    threads = [threading.Thread(target=claim, args=(name,)) for name in ("操作员甲", "操作员乙")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(1 for result in results if result["ok"]) == 1
    valid = _dispatches(client, round_key="2026-11-20T12:20", valid=True)
    assert len(valid) == 1
    assert valid[0]["接入地址"] == "https://sample3.example.com/api/noon"
    assert valid[0]["截止分钟"] == "12:30"
    assert valid[0]["来源版本"] == 2


def test_disabled_source_is_excluded_from_new_rounds(client: TestClient) -> None:
    _disable(client, 3)
    _generate(client, "13:00", "13:30")
    assert _items(client) == []
