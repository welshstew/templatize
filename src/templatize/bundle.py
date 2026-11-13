"""Zip bundle: template, .env, runner script and a short README."""

from __future__ import annotations

import io
import zipfile

from templatize.build import BuildResult
from templatize.emitters import get_emitter

_FIXED_TIME = (2020, 1, 1, 0, 0, 0)  # deterministic archives

README = """\
# Generated template

Files:

- `{template}`: the template ({engine})
- `.env`: the extracted values ({count} variables)
- {runners}

Render it (needs [uv](https://docs.astral.sh/uv/), which installs the dependencies):

    {command}

Or with pip:

    pip install jinja2 python-dotenv
    python render.py -o output.txt

Edit `.env` to change values. Round-trip check when generated: {verdict}.
"""


def bundle_readme(result: BuildResult) -> str:
    emitter = get_emitter(result.engine)
    return README.format(
        template=result.template.path,
        engine=result.engine,
        count=len(result.ir.variables),
        runners=", ".join(
            f"`{f.path}`: renders the template with the values" for f in result.runner
        )
        or "no runner script",
        command=emitter.run_command(),
        verdict="PASS" if result.roundtrip.ok else "FAIL",
    )


def make_zip(result: BuildResult) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in [*result.files]:
            info = zipfile.ZipInfo(f.path, date_time=_FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if f.path.endswith(".py") else 0o644) << 16
            zf.writestr(info, f.content.encode("utf-8", "surrogatepass"))
        info = zipfile.ZipInfo("README.md", date_time=_FIXED_TIME)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        zf.writestr(info, bundle_readme(result))
    return buf.getvalue()
