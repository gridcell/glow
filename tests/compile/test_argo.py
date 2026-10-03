import json
from dataclasses import replace

import pytest
import yaml

from glow.compile import CompileError, CompileOptions, compile_workflow
from glow.compile.argo import _item_value
from glow.compile.encoding import decode_json, decode_text
from glow.types import Group, Scalar, Unknown
from glow.validate import Code
from tests.compile.conftest import (
    COMPILED,
    EXAMPLES,
    FIXTURES,
    OPTIONS,
    IrFromYaml,
    arguments,
    compiled,
    load_ir,
    task,
    template,
)

SECURITY_CONTEXT = {
    "runAsNonRoot": True,
    "readOnlyRootFilesystem": True,
    "allowPrivilegeEscalation": False,
    "capabilities": {"drop": ["ALL"]},
    "seccompProfile": {"type": "RuntimeDefault"},
}


def compiled_ir(workflow) -> dict:
    return yaml.safe_load(compile_workflow(workflow, OPTIONS).to_yaml())


def compile_errors(workflow, options: CompileOptions = OPTIONS) -> list:
    with pytest.raises(CompileError) as caught:
        compile_workflow(workflow, options)
    return caught.value.errors


def test_sst_matches_appendix_d() -> None:
    workflow = compiled(EXAMPLES / "sst-ingest.yaml")
    assert workflow["kind"] == "Workflow"
    assert workflow["metadata"] == {"generateName": "sst-ingest-", "namespace": "tenant-acme"}
    spec = workflow["spec"]
    assert spec["entrypoint"] == "main"
    assert spec["serviceAccountName"] == "glow-runner"
    assert [p["name"] for p in spec["arguments"]["parameters"]] == [
        "source",
        "dest",
        "collection",
        "color_table",
    ]
    main = template(workflow, "main")
    assert [t["name"] for t in main["dag"]["tasks"]] == ["items", "per-item", "publish"]
    assert task(main, "items")["template"] == "builtin-fs-group"
    per_item = task(main, "per-item")
    assert per_item["template"] == "per-item-block"
    assert per_item["depends"] == "items"
    assert per_item["withParam"] == "{{tasks.items.outputs.parameters.groups}}"
    assert arguments(per_item)["g"] == "{{item}}"
    publish = task(main, "publish")
    assert publish["depends"] == "per-item"
    assert arguments(publish)["upstream-per-item"] == (
        '{"outputs":{"item":{{tasks.per-item.outputs.parameters.item}}},"skipped":false}'
    )

    block = template(workflow, "per-item-block")
    assert [t["name"] for t in block["dag"]["tasks"]] == ["cog", "thumb", "render", "item"]
    assert "depends" not in task(block, "cog")
    assert task(block, "thumb")["depends"] == "cog"
    assert task(block, "item")["depends"] == "cog && thumb && render"
    assert block["outputs"]["parameters"] == [
        {"name": "item", "valueFrom": {"parameter": "{{tasks.item.outputs.parameters.item}}"}}
    ]
    tools = [t["name"] for t in spec["templates"] if "container" in t]
    assert tools == [
        "builtin-fs-group",
        "tool-gdal-translate",
        "tool-gdal-dem-color-relief",
        "tool-prescient-render-from-color-table",
        "tool-stac-item",
        "tool-stac-publish",
    ]


def test_tool_template_has_the_glow_exec_pod_shape() -> None:
    workflow = compiled(EXAMPLES / "sst-ingest.yaml")
    cog = template(workflow, "tool-gdal-translate")
    (init,) = cog["initContainers"]
    assert init["image"] == "ghcr.io/sparkgeo/glow-exec:test"
    assert init["command"] == ["/glow-exec", "install", "/glow/exec"]
    container = cog["container"]
    assert container["image"] == "local/gdal:dev"
    assert container["command"] == ["/glow/exec", "run", "--tool", "gdal.translate@1", "--"]
    assert container["args"] == ["gdal_translate_wrapper"]
    assert container["securityContext"] == SECURITY_CONTEXT
    assert init["securityContext"] == SECURITY_CONTEXT
    assert {m["mountPath"] for m in container["volumeMounts"]} == {"/glow", "/work"}
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["GLOW_RUN_PREFIX"] == "{{inputs.parameters.run-prefix}}"
    assert decode_json(env["GLOW_MANIFEST"])["name"] == "gdal.translate"
    assert cog["outputs"]["parameters"] == [
        {"name": "outputs", "valueFrom": {"path": "/work/outputs.resolved.json"}},
        {"name": "result", "valueFrom": {"path": "/work/outputs/result.json", "default": "null"}},
    ]
    assert "when" not in json.dumps(workflow)


def test_run_prefix_nests_per_fan_out_item() -> None:
    workflow = compiled(EXAMPLES / "sst-ingest.yaml")
    main = template(workflow, "main")
    top = "s3://artifact-bucket-acme/runs/{{workflow.uid}}/steps"
    assert arguments(task(main, "items"))["run-prefix"] == f"{top}/items"
    assert arguments(task(main, "per-item"))["run-prefix"] == f"{top}/per-item/{{{{item.key}}}}"
    cog = task(template(workflow, "per-item-block"), "cog")
    assert arguments(cog)["run-prefix"] == "{{inputs.parameters.run-prefix}}/cog"


@pytest.mark.parametrize("name", sorted(COMPILED))
def test_every_raw_with_decodes_to_the_with_block(name: str) -> None:
    workflow_ir = load_ir(COMPILED[name])
    workflow = compiled(COMPILED[name])
    by_name = {step.id.replace("_", "-"): step for step in workflow_ir.steps}
    seen = 0
    for argo_template in workflow["spec"]["templates"]:
        for dag_task in argo_template.get("dag", {}).get("tasks", []):
            raw_with = arguments(dag_task).get("raw-with")
            if raw_with is not None:
                assert decode_json(raw_with) == by_name[dag_task["name"]].raw_with
                seen += 1
    assert seen == sum(1 for step in workflow_ir.steps if step.tool_spec is not None)


def test_raw_with_keeps_expressions_unevaluated() -> None:
    workflow = compiled(EXAMPLES / "sst-ingest.yaml")
    item = task(template(workflow, "per-item-block"), "item")
    raw_with = decode_json(arguments(item)["raw-with"])
    assert raw_with["id"] == "sst-${{ g.key }}"
    assert raw_with["assets"]["data"]["href"] == "${{ steps.cog.outputs.result }}"


def test_scope_carries_inputs_and_the_loop_variable() -> None:
    workflow = compiled(EXAMPLES / "sst-ingest.yaml")
    cog = task(template(workflow, "per-item-block"), "cog")
    scope = arguments(cog)["scope"]
    assert scope.startswith('{"inputs":{"source":{{workflow.parameters.source}},')
    assert scope.endswith(',"g":{{inputs.parameters.g}}}')


def test_block_if_reaches_every_member_with_its_upstream() -> None:
    workflow = compiled(EXAMPLES / "minimal-if-script.yaml")
    band_cog = task(template(workflow, "per-band-block"), "band-cog")
    assert arguments(band_cog)["if"] == "steps.count.outputs.total > 0"
    assert arguments(band_cog)["upstream-count"] == "{{inputs.parameters.upstream-count}}"
    preview = task(template(workflow, "per-scene-block"), "preview")
    assert arguments(preview)["if"] == "(steps.count.outputs.total > 0) && (inputs.make_previews)"
    env = {e["name"] for e in template(workflow, "tool-gdal-translate")["container"]["env"]}
    assert "GLOW_UPSTREAM_count" in env


def test_max_parallelism_wraps_the_fan_out() -> None:
    workflow = compiled(EXAMPLES / "minimal-if-script.yaml")
    main = template(workflow, "main")
    per_scene = task(main, "per-scene")
    assert per_scene["template"] == "per-scene-fanout"
    assert arguments(per_scene) == {
        "task-count-outputs": "{{tasks.count.outputs.parameters.outputs}}"
    }
    wrapper = template(workflow, "per-scene-fanout")
    assert wrapper["parallelism"] == 4
    inner = task(wrapper, "per-scene")
    assert inner["withParam"] == "{{workflow.parameters.scenes}}"
    assert arguments(inner)["upstream-count"] == "{{inputs.parameters.task-count-outputs}}"
    assert wrapper["outputs"]["parameters"] == [
        {
            "name": "previews",
            "valueFrom": {"parameter": "{{tasks.per-scene.outputs.parameters.previews}}"},
        }
    ]
    names = [t["name"] for t in workflow["spec"]["templates"]]
    assert names.index("per-scene-fanout") < names.index("per-scene-block")


def test_nested_for_each_reads_a_field_of_the_loop_variable() -> None:
    workflow = compiled(EXAMPLES / "minimal-if-script.yaml")
    per_band = task(template(workflow, "per-scene-block"), "per-band")
    assert per_band["withParam"] == "{{=toJson(jsonpath(inputs.parameters.scene, '$.bands'))}}"


def test_single_step_for_each_uses_with_param_on_the_tool() -> None:
    workflow = compiled(FIXTURES / "for-each-step.yaml")
    main = template(workflow, "main")
    info = task(main, "info")
    assert info["template"] == "tool-gdal-info"
    assert info["withParam"] == "{{tasks.tiffs.outputs.parameters.files}}"
    assert arguments(info)["scope"].endswith(',"tif":{{item}}}')
    summary = task(main, "summary")
    assert arguments(summary)["upstream-info"] == (
        '{"outputs":{"info":{{tasks.info.outputs.parameters.info}}},"skipped":false}'
    )


def test_second_configuration_of_a_tool_gets_its_own_template() -> None:
    workflow = compiled(FIXTURES / "resources.yaml")
    assert template(workflow, "tool-gdal-translate")["activeDeadlineSeconds"] == 5400
    small = template(workflow, "tool-gdal-translate-small")
    assert small["activeDeadlineSeconds"] == 600
    assert "resources" not in small["container"]


def test_resources_timeout_retries_and_staging() -> None:
    workflow = compiled(FIXTURES / "resources.yaml")
    big = template(workflow, "tool-gdal-translate")
    assert big["container"]["resources"] == {
        "requests": {"cpu": "2", "memory": "8Gi"},
        "limits": {"cpu": "4", "memory": "8Gi", "nvidia.com/gpu": "1"},
    }
    assert big["retryStrategy"] == {"limit": "2", "retryPolicy": "OnError"}
    env = {e["name"]: e["value"] for e in big["container"]["env"]}
    assert env["GLOW_STAGING"] == "auto"


def test_secrets_mount_on_the_declaring_template_only() -> None:
    workflow = compiled(FIXTURES / "secrets.yaml")
    cog = template(workflow, "tool-gdal-translate")
    secrets = [v for v in cog["volumes"] if "secret" in v]
    assert secrets == [
        {"name": "secret-0", "secret": {"secretName": "aws-creds"}},
        {"name": "secret-1", "secret": {"secretName": "api-token"}},
    ]
    mounts = {m["mountPath"]: m.get("readOnly") for m in cog["container"]["volumeMounts"]}
    assert mounts["/secrets/aws-creds"] is True and mounts["/secrets/api-token"] is True
    info = template(workflow, "tool-gdal-info")
    assert not any("secret" in v for v in info["volumes"])


def test_script_step_runs_on_the_sandbox_image() -> None:
    workflow = compiled(FIXTURES / "script-step.yaml")
    score_template = template(workflow, "script-score")
    container = score_template["container"]
    assert container["image"] == "ghcr.io/sparkgeo/glow-sandbox:test"
    assert container["command"] == ["/glow/exec", "run", "--tool", "glow.script@1", "--"]
    assert container["args"][-1] == "{{inputs.parameters.script}}"
    assert "python3 /work/script" in container["args"][2]
    spec = decode_json({e["name"]: e["value"] for e in container["env"]}["GLOW_MANIFEST"])
    assert spec["inputs"] == {
        "threshold": {"type": "number"},
        "label": {"type": "string"},
        "options": {"type": "object"},
    }
    assert spec["outputs"] == {"score": {"type": "number"}, "tags": {"type": "array"}}
    main = template(workflow, "main")
    score = task(main, "score")
    assert decode_text(arguments(score)["script"]).startswith("import json\n")
    report = task(main, "report")
    assert arguments(report)["if"] == "steps.score.outputs.score > inputs.threshold"
    assert "bash /work/script" in template(workflow, "script-report")["container"]["args"][2]


def test_workflow_parameters_carry_json_defaults() -> None:
    workflow = compiled(FIXTURES / "script-step.yaml")
    assert workflow["spec"]["arguments"]["parameters"] == [
        {"name": "threshold", "value": "0.5"},
        {"name": "label", "value": '"{scene}"'},
    ]


def test_string_items_are_quoted_as_json() -> None:
    assert _item_value(Scalar({"type": "string", "format": "uri"})) == "{{=toJson(item)}}"
    assert _item_value(Group(())) == "{{item}}"
    assert _item_value(Unknown("later")) == "{{item}}"


@pytest.mark.parametrize(
    ("fixture", "location", "words"),
    [
        ("block-outer-step-output.yaml", "cog.with.subdataset", "steps.items is outside block"),
        ("block-outer-let.yaml", "cog.with.source", "let binding 'path'"),
        ("block-outer-loop-var.yaml", "cog.with.subdataset", "loop variable 'scene'"),
    ],
)
def test_outer_references_in_blocks_are_not_yet_supported(
    fixture: str, location: str, words: str
) -> None:
    (error,) = compile_errors(load_ir(FIXTURES / fixture))
    assert error.code == Code.NOT_YET_SUPPORTED
    assert error.location == location
    assert words in error.render()


BLOCK = """
name: t
inputs:
  sources: { type: array }
steps:
  - id: first
    uses: gdal.info@1
    with:
      source: ${{ inputs.sources[0] }}
  - id: per_source
    for_each: OPERAND
    as: s
    BLOCK_IF
    steps:
      - id: info
        uses: gdal.info@1
        with:
          source: ${{ s.path }}
    outputs:
      info: OUTPUT
"""


def block(
    operand: str = "${{ inputs.sources }}",
    block_if: str = "",
    output: str = "${{ steps.info.outputs.info }}",
) -> str:
    return BLOCK.replace("OPERAND", operand).replace("BLOCK_IF", block_if).replace("OUTPUT", output)


@pytest.mark.parametrize(
    ("text", "location"),
    [
        (block(operand="${{ inputs.sources.filter(x, x.ok) }}"), "per_source.for_each"),
        (block(output="${{ [steps.info.outputs.info] }}"), "per_source.outputs.info"),
    ],
)
def test_unsupported_block_shapes(ir_from_yaml: IrFromYaml, text: str, location: str) -> None:
    errors = compile_errors(ir_from_yaml(text))
    assert [(e.code, e.location) for e in errors] == [(Code.NOT_YET_SUPPORTED, location)]


def test_block_if_on_inputs_and_steps_compiles(ir_from_yaml: IrFromYaml) -> None:
    text = block(
        block_if="if: ${{ inputs.sources.size() > 0 && steps.first.outputs.info != null }}"
    )
    workflow = compiled_ir(ir_from_yaml(text))
    per_source = task(template(workflow, "main"), "per-source")
    assert per_source["depends"] == "first"
    assert arguments(per_source)["upstream-first"] == "{{tasks.first.outputs.parameters.outputs}}"


def test_block_if_on_an_outer_loop_variable_is_not_yet_supported(
    ir_from_yaml: IrFromYaml,
) -> None:
    workflow = ir_from_yaml(
        """
name: t
inputs:
  scenes: { type: array }
steps:
  - id: per_scene
    for_each: ${{ inputs.scenes }}
    as: scene
    steps:
      - id: per_band
        if: ${{ scene.ok }}
        for_each: ${{ scene.bands }}
        as: band
        steps:
          - id: info
            uses: gdal.info@1
            with:
              source: ${{ band.path }}
        outputs:
          infos: ${{ steps.info.outputs.info }}
    outputs:
      infos: ${{ steps.per_band.outputs.infos }}
"""
    )
    (error,) = compile_errors(workflow)
    assert (error.code, error.location) == (Code.NOT_YET_SUPPORTED, "per_band.if")
    assert "loop variable 'scene'" in error.render()


def test_results_reference_is_not_yet_supported(ir_from_yaml: IrFromYaml) -> None:
    workflow = ir_from_yaml(
        block()
        + """
  - id: after
    uses: gdal.info@1
    with:
      source: ${{ steps.per_source.results[0] }}
"""
    )
    (error,) = compile_errors(workflow)
    assert error.location == "after.with.source"
    assert "results" in error.render()


def test_if_with_braces_inside_is_not_yet_supported(ir_from_yaml: IrFromYaml) -> None:
    workflow = ir_from_yaml(
        "name: t\ninputs:\n  s: { type: string }\nsteps:\n"
        "  - id: a\n    if: \"${{ inputs.s == '{{' }}\"\n    uses: gdal.info@1\n"
        "    with:\n      source: ${{ inputs.s }}\n"
    )
    (error,) = compile_errors(workflow)
    assert (error.code, error.location) == (Code.NOT_YET_SUPPORTED, "a.if")


def test_local_images_need_the_flag() -> None:
    workflow = load_ir(EXAMPLES / "sst-ingest.yaml")
    errors = compile_errors(workflow, replace(OPTIONS, allow_local_images=False))
    assert {e.code for e in errors} == {Code.LOCAL_IMAGE}
    assert [e.location for e in errors] == [
        "cog.uses",
        "thumb.uses",
        "render.uses",
        "item.uses",
        "publish.uses",
    ]


def test_pinned_images_are_referenced_by_digest() -> None:
    workflow = load_ir(FIXTURES / "secrets.yaml")
    digest = "sha256:" + "a" * 64
    steps = [
        step.model_copy(update={"tool": step.tool.model_copy(update={"digest": digest})})
        if step.tool is not None
        else step
        for step in workflow.steps
    ]
    pinned = workflow.model_copy(update={"steps": steps})
    out = compile_workflow(pinned, replace(OPTIONS, allow_local_images=False)).to_yaml()
    assert f"image: local/gdal:dev@{digest}" in out


def test_step_ids_that_differ_only_in_case_collide(ir_from_yaml: IrFromYaml) -> None:
    workflow = ir_from_yaml(
        "name: t\ninputs:\n  s: { type: file }\nsteps:\n"
        "  - id: Cog\n    uses: gdal.info@1\n    with:\n      source: ${{ inputs.s }}\n"
        "  - id: cog\n    uses: gdal.info@1\n    with:\n      source: ${{ inputs.s }}\n"
    )
    (error,) = compile_errors(workflow)
    assert error.code == Code.NAME_COLLISION
    assert "'cog' and 'Cog' both become Argo task 'cog'" in error.render()


def test_identical_steps_share_one_template(ir_from_yaml: IrFromYaml) -> None:
    workflow = ir_from_yaml(
        "name: t\ninputs:\n  s: { type: file }\nsteps:\n"
        "  - id: a\n    uses: gdal.info@1\n    with:\n      source: ${{ inputs.s }}\n"
        "  - id: b\n    uses: gdal.info@1\n    with:\n      source: ${{ inputs.s }}\n"
    )
    out = compile_workflow(workflow, OPTIONS)
    assert [t.name for t in out.templates] == ["main", "tool-gdal-info"]


@pytest.mark.parametrize(
    "change",
    [
        {"namespace": "Not_A_Namespace"},
        {"service_account": "bad name"},
        {"run_prefix": "relative/path"},
        {"run_prefix": "s3://bucket/{{workflow.name}}"},
        {"glow_exec_image": "image with space"},
        {"sandbox_image": "img{{x}}"},
    ],
)
def test_unsafe_options_are_refused(change: dict) -> None:
    with pytest.raises(ValueError):
        replace(OPTIONS, **change)


def test_run_prefix_loses_its_trailing_slash() -> None:
    assert replace(OPTIONS, run_prefix="s3://bucket/glow/").run_prefix == "s3://bucket/glow"
