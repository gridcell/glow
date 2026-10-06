from glow.models import Workflow
from glow.validate import scopes
from glow.validate.scopes import InputSymbol, LetSymbol, LoopSymbol, StepSymbol

WORKFLOW = Workflow.model_validate(
    {
        "name": "t",
        "inputs": {"xs": {"type": "array"}},
        "steps": [
            {"id": "a", "run": "true", "outputs": {"n": {"type": "integer"}}},
            {
                "id": "b",
                "for_each": "${{ inputs.xs }}",
                "as": "x",
                "let": {"first": "${{ x.name }}", "second": "${{ first }}"},
                "steps": [
                    {
                        "id": "c",
                        "run": "true",
                        "with": {"v": "${{ steps.a.outputs.n }}"},
                        "outputs": {"o": {"type": "string"}},
                    },
                    {
                        "id": "d",
                        "run": "true",
                        "with": {"v": "${{ steps.c.outputs.o }}"},
                        "outputs": {"o": {"type": "string"}},
                    },
                ],
                "outputs": {"all": "${{ steps.d.outputs.o }}"},
            },
        ],
    }
)


def symbols(table: scopes.SymbolTable, step_id: str, field: tuple[str | int, ...]) -> list[object]:
    site = table.site(step_id, field)
    assert site is not None
    return [use.symbol for span in site.spans for use in span.uses]


def test_symbols_resolve_by_scope() -> None:
    table = scopes.build(WORKFLOW, lambda step_id: None)
    assert table.errors == []
    assert symbols(table, "b", ("for_each",)) == [InputSymbol("xs")]
    assert symbols(table, "b", ("let", "first")) == [LoopSymbol("b")]
    assert symbols(table, "b", ("let", "second")) == [LetSymbol("b", "first")]
    assert symbols(table, "d", ("with", "v")) == [StepSymbol("c")]
    assert symbols(table, "b", ("outputs", "all")) == [StepSymbol("d")]


def test_dependencies_are_between_siblings() -> None:
    table = scopes.build(WORKFLOW, lambda step_id: None)
    pairs = [(dep.consumer, dep.producer) for dep in table.dependencies]
    # c, inside block b, uses a: the dependency is promoted to b.
    assert pairs == [("b", "a"), ("d", "c")]
    assert table.parents == {"a": None, "b": None, "c": "b", "d": "b"}
    assert table.lists == {None: ["a", "b"], "b": ["c", "d"]}


def test_output_names_are_checked_when_known() -> None:
    table = scopes.build(WORKFLOW, lambda step_id: {"other"} if step_id == "a" else None)
    assert [error.location for error in table.errors] == ["c.with.v"]


def test_format_field() -> None:
    assert scopes.format_field(("with", "assets", "data", "href")) == "with.assets.data.href"
    assert scopes.format_field(("with", "size", 0)) == "with.size[0]"
