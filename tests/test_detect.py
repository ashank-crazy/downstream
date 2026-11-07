from downstream.contracts import Contract, normalize, term_in
from downstream.detect import RoomView, RuleAnalyzer
from downstream.demo import ROOMS


def views() -> list[RoomView]:
    return [RoomView(f"r-{n}", n, Contract.parse(c)) for n, _, c in ROOMS]


def by_name(name: str) -> RoomView:
    return next(v for v in views() if v.name == name)


def test_contract_parse_and_render():
    c = Contract.parse("Owns: orders-api, user_id\nDepends on: postgres cluster and ci")
    assert c.owns == ["orders-api", "user_id"]
    assert c.depends == ["postgres cluster", "ci"]
    assert "`orders-api`" in c.render()


def test_contract_nothing_is_empty():
    assert Contract.parse("Owns: ci\nDepends on: nothing").depends == []


def test_terms_match_across_separators():
    norm = normalize("We're renaming `user-id` in the Orders API")
    assert term_in("user_id", norm)
    assert term_in("orders api", norm)
    assert term_in("order", norm)  # plural tolerated
    assert not term_in("order id", norm)


def test_breaking_change_reaches_dependents_only():
    v = RuleAnalyzer().analyze(
        "Heads up: in the orders-api v2 response we're renaming `user_id` to `account_id`.",
        by_name("api-platform"), views(),
    )
    hit = {i.room_id for i in v.impacts}
    assert v.is_change
    assert hit == {"r-mobile-app", "r-data-insights", "r-billing"}
    mobile = next(i for i in v.impacts if i.room_id == "r-mobile-app")
    assert "owned by #api-platform" in mobile.reason


def test_question_without_change_word_stays_quiet():
    v = RuleAnalyzer().analyze(
        "Anyone know why the user_id index on orders got slow this morning?",
        by_name("api-platform"), views(),
    )
    assert not v.is_change


def test_change_nobody_depends_on_stays_quiet():
    v = RuleAnalyzer().analyze("We're migrating the revenue dashboard to Grafana.",
                               by_name("data-insights"), views())
    assert not v.is_change


def test_infra_change_reaches_api_and_mobile():
    v = RuleAnalyzer().analyze(
        "Postgres cluster maintenance window Saturday 02:00, push notifications paused too.",
        by_name("infra"), views(),
    )
    assert {i.room_id for i in v.impacts} == {"r-api-platform", "r-mobile-app"}


def test_origin_room_never_targets_itself():
    v = RuleAnalyzer().analyze("We're deprecating user_id.", by_name("mobile-app"), views())
    assert "r-mobile-app" not in {i.room_id for i in v.impacts}
