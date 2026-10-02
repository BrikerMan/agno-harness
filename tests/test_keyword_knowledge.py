"""Chinese keyword retrieval: bigram BM25, field weights, filters the model may get wrong."""

import json

from agno.agent import Agent
from agno.agent._default_tools import create_knowledge_search_tool
from agno.knowledge.types import KnowledgeFilter

from agno_harness import CjkKeywordScorer, KeywordKnowledge
from agno_harness.knowledge import tokenize

STANDARDS = [
    {
        "code": "PC-G-113",
        "level": "general",
        "category": "设备设施",
        "equipment": "管架",
        "criterion": "支吊架锈蚀、变形、断裂、脱落",
        "aliases": ["管架立柱锈蚀"],
    },
    {
        "code": "PC-G-082",
        "level": "general",
        "category": "设备设施",
        "equipment": "绝缘接头",
        "criterion": "外壳变形锈蚀，极柱放电",
        "aliases": [],
    },
    {
        "code": "PC-S-06",
        "level": "significant",
        "category": "安全管理",
        "equipment": "",
        "criterion": "占用、堵塞、封闭疏散通道、安全出口、消防车通道",
        "aliases": ["消防通道被占"],
    },
    {
        "code": "PC-G-133",
        "level": "general",
        "category": "其他",
        "equipment": "",
        "criterion": "其他",
        "aliases": [],
        "is_active": False,
    },
]

FIELDS = {"equipment": 3, "criterion": 3, "aliases": 2, "category": 1}


class LevelAwareKnowledge(KeywordKnowledge):
    def normalize_filter_value(self, key, value):
        if key == "level":
            return {"一般": "general", "较大": "significant", "重大": "major"}.get(value, value)
        return value


def make_kb(**kwargs):
    return LevelAwareKnowledge(
        lambda: STANDARDS,
        fields=FIELDS,
        id_field="code",
        filter_fields=("level", "category"),
        include=lambda d: d.get("is_active", True),
        **kwargs,
    )


def test_tokenize_cuts_cjk_into_bigrams_and_keeps_latin_words():
    terms = [t.term for t in tokenize("管架立柱 GDS 3号")]
    assert terms == ["管架", "架立", "立柱", "gds", "3", "号"]


def test_alias_and_field_weight_rank_the_right_standard_first():
    hits = make_kb().search("管架立柱锈蚀")
    assert hits[0]["code"] == "PC-G-113"
    assert "管架立柱锈蚀" in hits[0]["matched_terms"]
    assert hits[0]["score"] > hits[1]["score"]


def test_matched_terms_are_joined_back_into_words():
    hits = make_kb().search("消防车通道 占用")
    top = hits[0]
    assert top["code"] == "PC-S-06"
    assert set(top["matched_terms"]) == {"消防车通道", "占用"}


def test_inactive_documents_never_come_back():
    assert all(h["code"] != "PC-G-133" for h in make_kb().search("其他"))


def test_filters_narrow_results_and_accept_model_wording():
    hits = make_kb().search("锈蚀", filters={"level": "一般"})
    assert {h["code"] for h in hits} == {"PC-G-113", "PC-G-082"}
    assert make_kb().search("通道", filters={"level": ["较大"]})[0]["code"] == "PC-S-06"


def test_unknown_filter_keys_and_values_are_dropped_not_fatal(caplog):
    kb = make_kb()
    valid, dropped = kb.validate_filters(
        {"level": "superhigh", "owner": "张三", "category": "设备设施"}
    )
    assert valid == {"category": ["设备设施"]}
    assert set(dropped) == {"level=superhigh", "owner"}

    hits = kb.search("通道", filters={"area": "站场"})
    assert hits[0]["code"] == "PC-S-06"
    assert "ignored filters" in caplog.text


def test_blank_query_and_zero_limit_return_nothing():
    kb = make_kb()
    assert kb.search("   ") == []
    assert kb.search("锈蚀", num_documents=0) == []


def test_cache_holds_until_invalidate():
    rows = [dict(STANDARDS[0])]
    kb = KeywordKnowledge(lambda: rows, fields=FIELDS, id_field="code")
    assert kb.search("锈蚀")[0]["code"] == "PC-G-113"
    rows[0] = {**rows[0], "criterion": "法兰泄漏", "aliases": [], "equipment": "法兰"}
    assert kb.search("锈蚀")[0]["code"] == "PC-G-113"
    kb.invalidate()
    assert kb.search("锈蚀") == []


def test_scorer_breaks_ties_by_id():
    scorer = CjkKeywordScorer({"text": 1})
    docs = [{"id": "b", "text": "阀门"}, {"id": "a", "text": "阀门"}]
    assert [h.doc["id"] for h in scorer.rank(docs, "阀门", tie_key="id")] == ["a", "b"]


def test_plugs_into_agno_as_retriever_with_agentic_filters():
    kb = make_kb()
    agent = Agent(
        knowledge_retriever=kb.search, search_knowledge=True, enable_agentic_knowledge_filters=True
    )
    tool = create_knowledge_search_tool(agent, enable_agentic_filters=True)
    out = tool.entrypoint(query="锈蚀", filters=[KnowledgeFilter(key="level", value="一般")])
    docs = json.loads(out)
    assert [d["code"] for d in docs] == ["PC-G-113", "PC-G-082"]
    assert docs[0]["matched_terms"] == ["锈蚀"]
