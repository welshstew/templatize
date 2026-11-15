"""Table rows with aligned columns: loops whose fields are padded slots."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from templatize.build import LoopFieldSpec, LoopSpec, build, specs_from_loop_candidates
from templatize.emitters.jinja2 import Jinja2Emitter
from templatize.helm import build_chart
from templatize.ir import IRValidationError, Span
from templatize.loops import detect_loops
from templatize.suggest import suggest

INVOICE = (
    "Description                         Qty    Unit price       Amount\n"
    "------------------------------------------------------------------\n"
    "Website redesign                      1      4,200.00     4,200.00\n"
    "Hosting (12 months)                   1        360.00       360.00\n"
    "------------------------------------------------------------------\n"
)


def test_invoice_rows_are_a_table_loop() -> None:
    (lp,) = detect_loops(INVOICE, [])
    assert lp.name == "rows" and len(lp.items) == 2
    assert [(f.name, f.align) for f in lp.fields] == [
        ("description", "left"),
        ("qty", "right"),
        ("unit_price", "right"),
        ("amount", "right"),
    ]


def test_new_rows_line_up_under_the_header() -> None:
    built = build(INVOICE, [], loops=specs_from_loop_candidates(detect_loops(INVOICE, [])))
    assert built.roundtrip.ok
    values = yaml.safe_load(built.env.content)
    assert values["rows"][0] == {
        "description": "Website redesign",
        "qty": "1",
        "unit_price": "4,200.00",
        "amount": "4,200.00",
    }
    values["rows"].append(
        {
            "description": "Support retainer (monthly)",
            "qty": "12",
            "unit_price": "150.00",
            "amount": "1,800.00",
        }
    )
    out = Jinja2Emitter().render(built.template.content, values)
    assert "Support retainer (monthly)           12        150.00     1,800.00\n" in out


def test_tables_without_a_header_and_non_tables() -> None:
    text = (
        "alpha      10.0.0.1     ok\nbeta-long  10.0.0.22    failed\ngamma      10.0.0.3     ok\n"
    )
    (lp,) = detect_loops(text, [])
    assert [f.align for f in lp.fields] == ["left", "left", None]  # last column unpadded
    assert build(text, [], loops=specs_from_loop_candidates([lp])).roundtrip.ok
    # Two spaces between words in prose do not make a table.
    assert detect_loops("Hello  world, said Ann.\nGoodbye  then.\n", []) == []


def test_padded_fields_need_one_width() -> None:
    text = "ab  |\ncd  |\n"
    loop = LoopSpec(
        name="rows",
        items=[Span(start=0, end=5), Span(start=6, end=11)],
        fields=[
            LoopFieldSpec(
                name="x", spans=[Span(start=0, end=4), Span(start=6, end=9)], align="left"
            )
        ],
    )
    with pytest.raises((IRValidationError, ValueError), match="same width"):
        build(text, [], loops=[loop])


CONFIGMAP = (
    "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: hosts\ndata:\n  hosts: |\n"
    "    db        10.0.0.15    5432\n"
    "    cache     10.0.0.7     6379\n"
    "    search    10.0.0.122   9200\n"
)


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_helm_printf_pads_like_the_template(tmp_path: Path) -> None:
    loops = specs_from_loop_candidates(suggest(CONFIGMAP).loops)
    assert [lp.name for lp in loops] == ["rows"]
    chart = build_chart(CONFIGMAP, [], loops=loops)
    assert chart.ok, chart.roundtrips
    files = {f.path: f.content for f in chart.files}
    assert 'printf "%-8v"' in files["templates/configmap.yaml"]
    root = tmp_path / chart.chart_name
    for p, c in files.items():
        (root / p).parent.mkdir(parents=True, exist_ok=True)
        (root / p).write_text(c, encoding="utf-8", newline="")
    values = yaml.safe_load(files["values.yaml"])
    assert list(values["rows"][0]) == ["name", "ip_address", "number"]
    values["rows"].append({"name": "queue-primary", "ip_address": "10.0.0.9", "number": "5672"})
    (tmp_path / "more.yaml").write_text(yaml.safe_dump({"rows": values["rows"]}))
    helm = shutil.which("helm")
    assert helm
    out = subprocess.run(
        [helm, "template", "r", str(root), "-f", str(tmp_path / "more.yaml")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    hosts = yaml.safe_load(out.split("\n", 2)[2])["data"]["hosts"]
    assert hosts.splitlines()[-1] == "queue-primary  10.0.0.9     5672"
