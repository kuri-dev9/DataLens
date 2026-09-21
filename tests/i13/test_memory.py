from __future__ import annotations

import math

import pytest

from datalens.application.memory import MemoryService, plan_from_events, render_hints
from datalens.domain.memory import GlossaryEntry, PlanStep, Recall, Recipe
from datalens.infrastructure.embeddings import normalize
from datalens.infrastructure.sqlite_memory import SqliteMemoryStore
from datalens.ports.memory import EmbeddingUnavailable


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def store() -> SqliteMemoryStore:
    memory_store = SqliteMemoryStore(":memory:")
    yield memory_store
    memory_store.close()


class FakeEmbedder:
    """문자 단위 빈도를 벡터로 쓴다. 표현이 비슷하면 유사도가 높다."""

    ALPHABET = "가나다라마바사아자차카타파하PGW사용량트래픽"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    async def embed(self, text: str, timeout_seconds: float):
        self.calls.append(text)
        if self.fail:
            raise EmbeddingUnavailable("no embedder")
        counts = [float(text.count(letter)) for letter in self.ALPHABET]
        if not any(counts):
            counts[0] = 1.0
        return normalize(counts)

    async def ready(self, timeout_seconds: float) -> bool:
        return not self.fail

    async def close(self) -> None:
        return None


def steps() -> tuple[PlanStep, ...]:
    return (
        PlanStep("schema", {"action": "describe_table", "table": "PM_CEI_PGW_5M"}),
        PlanStep("query", {"table": "PM_CEI_PGW_5M", "group_by": ["PGW_ID"], "aggregations": ["sum(DATA_CEI_VALUE)"]}),
    )


def test_embeddings_round_trip_through_sqlite(store: SqliteMemoryStore) -> None:
    vector = normalize([1.0, 2.0, 2.0])
    recipe = store.upsert_recipe("PGW 사용량", "ko", steps(), vector, merge_threshold=0.99)
    assert recipe.tables == ("PM_CEI_PGW_5M",)
    found = store.search_recipes(vector, "ko", limit=3, threshold=0.5)
    assert len(found) == 1
    assert math.isclose(found[0][1], 1.0, abs_tol=1e-6)
    assert found[0][0].steps[1].intent["group_by"] == ["PGW_ID"]


def test_recipes_are_scoped_by_locale(store: SqliteMemoryStore) -> None:
    vector = normalize([1.0, 0.0])
    store.upsert_recipe("PGW 사용량", "ko", steps(), vector, merge_threshold=0.99)
    assert store.search_recipes(vector, "ja", limit=3, threshold=0.5) == ()
    assert len(store.search_recipes(vector, "ko", limit=3, threshold=0.5)) == 1


def test_near_duplicate_questions_merge_instead_of_piling_up(store: SqliteMemoryStore) -> None:
    first = store.upsert_recipe("PGW 사용량", "ko", steps(), normalize([1.0, 0.02]), merge_threshold=0.9)
    second = store.upsert_recipe("PGW별 사용량", "ko", steps(), normalize([1.0, 0.03]), merge_threshold=0.9)
    assert first.recipe_id == second.recipe_id
    assert second.question == "PGW별 사용량"
    assert len(store.list_recipes("ko", limit=10)) == 1


def test_reuse_outcome_moves_the_weight(store: SqliteMemoryStore) -> None:
    recipe = store.upsert_recipe("PGW 사용량", "ko", steps(), normalize([1.0]), merge_threshold=0.99)
    assert recipe.weight == 0.5
    store.record_reuse([recipe.recipe_id], succeeded=True)
    store.record_reuse([recipe.recipe_id], succeeded=True)
    assert store.get_recipe(recipe.recipe_id).weight > 0.7
    store.record_reuse([recipe.recipe_id], succeeded=False)
    store.record_reuse([recipe.recipe_id], succeeded=False)
    assert store.get_recipe(recipe.recipe_id).weight < 0.6


def test_ranking_prefers_a_proven_recipe_over_a_marginally_closer_one(store: SqliteMemoryStore) -> None:
    query = normalize([1.0, 0.0])
    # 질의 기준 유사도 0.80 vs 0.85. 가까운 쪽이 처음 쓰이는 사례라면 성적이 이긴다.
    proven = store.upsert_recipe("PGW 사용량", "ko", steps(), normalize([0.80, 0.60]), merge_threshold=0.999)
    for _ in range(5):
        store.record_reuse([proven.recipe_id], succeeded=True)
    fresh = store.upsert_recipe("PGW 트래픽", "ko", steps(), normalize([0.85, 0.5268]), merge_threshold=0.999)
    ranked = store.search_recipes(query, "ko", limit=2, threshold=0.1)
    assert [recipe.recipe_id for recipe, _ in ranked] == [proven.recipe_id, fresh.recipe_id]
    assert ranked[1][1] > ranked[0][1]  # 유사도만 보면 순서가 뒤집힌다


def test_prune_drops_the_weakest_recipes(store: SqliteMemoryStore) -> None:
    for index in range(5):
        vector = [0.0] * 5
        vector[index] = 1.0
        recipe = store.upsert_recipe(f"질문{index}", "ko", steps(), normalize(vector), merge_threshold=0.99)
        for _ in range(index):
            store.record_reuse([recipe.recipe_id], succeeded=True)
    assert store.prune(3) == 2
    remaining = {recipe.question for recipe in store.list_recipes("ko", limit=10)}
    assert remaining == {"질문2", "질문3", "질문4"}


def test_glossary_upsert_is_idempotent_per_locale(store: SqliteMemoryStore) -> None:
    vector = normalize([1.0, 1.0])
    store.upsert_term("PGW 사용량", "ko", vector, table="A", columns=["X"], note=None, source="manual")
    entry = store.upsert_term("PGW 사용량", "ko", vector, table="PM_CEI_PGW_5M", columns=["DATA_CEI_VALUE"], note="데이터 CEI", source="manual")
    assert len(store.list_terms("ko", limit=10)) == 1
    assert entry.table == "PM_CEI_PGW_5M"
    assert entry.columns == ("DATA_CEI_VALUE",)


@pytest.mark.anyio
async def test_service_recalls_what_it_recorded() -> None:
    service = MemoryService(SqliteMemoryStore(":memory:"), FakeEmbedder(), threshold=0.5, merge_threshold=0.999)
    await service.record_success("PGW별 사용량 구해줄래?", "ko", steps())
    recall = await service.recall("PGW 사용량 알려줘", "ko")
    assert recall.recipes
    assert recall.recipes[0][0].steps[1].intent["table"] == "PM_CEI_PGW_5M"
    await service.close()


@pytest.mark.anyio
async def test_service_degrades_quietly_without_an_embedder() -> None:
    service = MemoryService(SqliteMemoryStore(":memory:"), FakeEmbedder(fail=True))
    assert await service.record_success("PGW 사용량", "ko", steps()) is None
    recall = await service.recall("PGW 사용량", "ko")
    assert not recall
    assert render_hints(recall) is None
    await service.close()


@pytest.mark.anyio
async def test_empty_plan_is_not_recorded() -> None:
    embedder = FakeEmbedder()
    service = MemoryService(SqliteMemoryStore(":memory:"), embedder)
    assert await service.record_success("안녕?", "ko", ()) is None
    assert embedder.calls == []
    await service.close()


def test_hints_carry_the_plan_and_warn_against_copying() -> None:
    recipe = Recipe(1, "PGW별 트래픽량", "ko", steps(), ("PM_CEI_PGW_5M",), uses=3, successes=3)
    entry = GlossaryEntry(1, "PGW 사용량", "ko", "PM_CEI_PGW_5M", ("DATA_CEI_VALUE",), "데이터 CEI 값")
    hints = render_hints(Recall(recipes=((recipe, 0.88),), terms=((entry, 0.91),)))
    assert hints is not None
    assert "PGW 사용량 → PM_CEI_PGW_5M.DATA_CEI_VALUE (데이터 CEI 값)" in hints
    assert '"PGW별 트래픽량" (유사도 0.88, 재사용 3회)' in hints
    assert "조정해서 쓰세요" in hints
    assert "schema로 확인하세요" in hints
    assert "PM_CEI_PGW_5M" in hints


def test_plan_from_events_keeps_only_named_tools() -> None:
    plan = plan_from_events([
        {"tool": "schema", "intent": {"action": "list_tables"}},
        {"intent": {"table": "X"}},
        {"tool": "query", "intent": {}},
    ])
    assert [step.tool for step in plan] == ["schema", "query"]


@pytest.mark.anyio
async def test_chat_turn_records_its_plan_and_replays_it_next_time() -> None:
    from datalens.application.agent import AgentResult
    from datalens.application.chat import ChatApplicationService
    from datalens.application.sessions import SessionService
    from datalens.infrastructure.session_store import InMemorySessionStore

    store = InMemorySessionStore(60)
    sessions = SessionService(store)
    memory = MemoryService(SqliteMemoryStore(":memory:"), FakeEmbedder(), threshold=0.5, merge_threshold=0.999)
    seen: list[str | None] = []

    class Agent:
        async def run(self, session, message, deadline, event_sink=None, hints=None):
            seen.append(hints)
            return AgentResult(
                "완료", (), (), 2, 0, session,
                plan=({"tool": "schema", "intent": {"action": "describe_table", "table": "PM_CEI_PGW_5M"}},
                      {"tool": "query", "intent": {"table": "PM_CEI_PGW_5M", "group_by": ["PGW_ID"]}}),
            )

    chat = ChatApplicationService(sessions, Agent(), memory)
    first = store.create()
    await chat.handle(first, "PGW별 사용량 구해줄래?", "dlr_1", 0.0)
    second = store.create()
    result = await chat.handle(second, "PGW 사용량 알려줘", "dlr_2", 0.0)

    assert seen[0] is None  # 처음엔 기억이 없다
    assert seen[1] is not None and "PM_CEI_PGW_5M" in seen[1]
    assert result["metadata"]["memory"]["recipes"][0]["question"] == "PGW별 사용량 구해줄래?"
    await memory.close()


@pytest.mark.anyio
async def test_a_failed_turn_counts_against_the_recipe_it_reused() -> None:
    from datalens.application.agent import AgentQueryRejected, AgentResult
    from datalens.application.chat import ChatApplicationService
    from datalens.application.sessions import SessionService
    from datalens.infrastructure.session_store import InMemorySessionStore

    store = InMemorySessionStore(60)
    sessions = SessionService(store)
    backing = SqliteMemoryStore(":memory:")
    memory = MemoryService(backing, FakeEmbedder(), threshold=0.5, merge_threshold=0.999)

    class Agent:
        def __init__(self) -> None:
            self.fail = False

        async def run(self, session, message, deadline, event_sink=None, hints=None):
            if self.fail:
                raise AgentQueryRejected("UNKNOWN_COLUMN")
            return AgentResult("완료", (), (), 1, 0, session, plan=({"tool": "query", "intent": {"table": "T"}},))

    agent = Agent()
    chat = ChatApplicationService(sessions, agent, memory)
    await chat.handle(store.create(), "PGW 사용량", "dlr_1", 0.0)
    agent.fail = True
    with pytest.raises(AgentQueryRejected):
        await chat.handle(store.create(), "PGW 사용량 알려줘", "dlr_2", 0.0)

    recipe = backing.list_recipes("ko", limit=5)[0]
    assert (recipe.uses, recipe.successes) == (1, 0)
    assert recipe.weight < 0.5
    await memory.close()


def test_memory_endpoints_manage_the_glossary() -> None:
    from starlette.testclient import TestClient

    from datalens.api.app import build_app
    from datalens.config import Settings

    settings = Settings(api_key="k", queryforge_api_key="q")
    memory = MemoryService(SqliteMemoryStore(":memory:"), FakeEmbedder())
    headers = {"x-api-key": "k"}
    with TestClient(build_app(settings, memory=memory)) as client:
        created = client.post(
            "/v1/memory/terms",
            headers=headers,
            json={"term": "PGW 사용량", "table": "PM_CEI_PGW_5M", "columns": ["DATA_CEI_VALUE"], "note": "데이터 CEI"},
        )
        assert created.status_code == 201
        term_id = created.json()["term_id"]
        listed = client.get("/v1/memory/terms", headers=headers).json()["terms"]
        assert [entry["term"] for entry in listed] == ["PGW 사용량"]
        assert listed[0]["source"] == "manual"
        assert client.delete(f"/v1/memory/terms/{term_id}", headers=headers).status_code == 204
        assert client.get("/v1/memory/terms", headers=headers).json()["terms"] == []


def test_memory_endpoints_report_when_memory_is_off() -> None:
    from starlette.testclient import TestClient

    from datalens.api.app import build_app
    from datalens.config import Settings

    with TestClient(build_app(Settings(api_key="k", queryforge_api_key="q"))) as client:
        response = client.get("/v1/memory/recipes", headers={"x-api-key": "k"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DL_MEMORY_DISABLED"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("api", "path", "body"),
    [
        ("ollama", "/api/embed", {"embeddings": [[3.0, 4.0]]}),
        ("ollama", "/api/embed", {"embedding": [3.0, 4.0]}),
        ("openai", "/v1/embeddings", {"data": [{"embedding": [3.0, 4.0]}]}),
    ],
)
async def test_embedder_speaks_both_api_shapes(api, path, body) -> None:
    import httpx

    from datalens.infrastructure.embeddings import HttpEmbedder

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    embedder = HttpEmbedder("http://embed:11435/", "bge-m3", api=api, client=client)
    vector = await embedder.embed("PGW 사용량", 5.0)
    await client.aclose()

    assert seen[0].url.path == path
    assert vector == (0.6, 0.8)  # 저장 전 단위 벡터로 정규화된다


@pytest.mark.anyio
async def test_embedder_can_point_at_a_separate_server() -> None:
    import httpx

    from datalens.infrastructure.embeddings import HttpEmbedder

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0]]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    embedder = HttpEmbedder("http://cpu-ollama:11435", "bge-m3", api_key="secret", client=client)
    await embedder.embed("질문", 5.0)
    await client.aclose()

    assert str(seen[0].url) == "http://cpu-ollama:11435/api/embed"
    assert seen[0].headers["authorization"] == "Bearer secret"


@pytest.mark.anyio
async def test_embedder_reports_unavailable_instead_of_raising_transport_errors() -> None:
    import httpx

    from datalens.infrastructure.embeddings import HttpEmbedder

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    embedder = HttpEmbedder("http://down:11435", "bge-m3", client=client)
    with pytest.raises(EmbeddingUnavailable):
        await embedder.embed("질문", 5.0)
    assert await embedder.ready(5.0) is False
    await client.aclose()


def test_embedding_url_falls_back_to_the_llm_host() -> None:
    from datalens.config import Settings

    shared = Settings(api_key="k", queryforge_api_key="q", ollama_base_url="http://gpu:11434")
    assert shared.embedding_url() == "http://gpu:11434"
    split = Settings(
        api_key="k",
        queryforge_api_key="q",
        ollama_base_url="http://gpu:11434",
        embedding_base_url="http://cpu:11435/",
    )
    assert split.embedding_url() == "http://cpu:11435"
