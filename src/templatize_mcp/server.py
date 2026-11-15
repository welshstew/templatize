"""MCP server exposing templatize (official Python MCP SDK).

    templatize-mcp                      # stdio (Claude Desktop / Claude Code launch it)
    templatize-mcp --transport http     # streamable HTTP on 127.0.0.1:8001/mcp

The web API also serves streamable HTTP at ``/mcp`` (see templatize_api.app).
Logging goes to stderr only (stdout is the stdio transport) and never includes
tool arguments.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from templatize import __version__
from templatize.build import VariableInput
from templatize.doctypes import TYPES
from templatize_mcp import tools

INSTRUCTIONS = """\
templatize turns a plain-text document (config file, manifest, email, contract...)
into a Jinja2 template plus a .env file of extracted values and a render.py script,
and verifies that rendering the template reproduces the input byte for byte.

Typical flow:
1. suggest_parameters(text, variants?) -> scored candidate variables. Passing 1-3
   other versions of the same document as `variants` makes the parts that differ
   very-high-confidence suggestions.
2. generate_template(text, variables=[{name, value}...]) -> template, .env,
   render.py and a round-trip verdict. Identify variables by `value` (the exact text);
   you never need character offsets. Omit `variables` to accept suggestions above
   `min_confidence` automatically, including `loops`: repeated blocks (list items,
   SQL rows) become {% for %} loops and their values go to values.yaml instead of .env.
3. render_template / validate_template to try other values or check a template
   that you edited.
Everything runs locally; nothing is stored or sent anywhere.
"""

log = logging.getLogger("templatize.mcp")


@contextmanager
def _tool_errors(tool: str) -> Iterator[None]:
    """Privacy boundary for tool calls.

    Bad input becomes a ToolError whose message the client sees. Anything else is
    logged by exception *type* only (messages and tracebacks can quote user text) and
    reported generically. The SDK therefore never logs a raw exception from our code.
    """
    try:
        yield
    except tools.ToolInputError as exc:
        raise ToolError(str(exc)) from None
    except Exception as exc:
        log.error("tool %s crashed: %s", tool, type(exc).__name__)
        raise ToolError(f"internal error ({type(exc).__name__})") from None


def quiet_sdk_logging() -> None:
    """The SDK logs failed tool calls at INFO including their error text and argument
    names; keep its loggers at WARNING so user text never reaches the logs."""
    logging.getLogger("mcp").setLevel(logging.WARNING)


READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)

DEFAULT_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]


def _schema_text(schema: str | dict[str, Any] | None) -> str | None:
    """A schema passed as an object (MCP clients parse JSON arguments) or as text."""
    return json.dumps(schema) if isinstance(schema, dict) else schema


SCHEMA_HELP = (
    "A JSON Schema (an object, or JSON/YAML text) the document follows: its formats, consts, "
    "defaults and titles sharpen the suggestions. A schema the document references by "
    "URL is never downloaded; if you have its text, pass it here."
)
DOCUMENT_TYPE_HELP = (
    "Confirm what kind of document this is, so its format rules apply (omit to detect it; "
    "the result's document_type says what was detected). One of: "
    + ", ".join(t.id for t in TYPES)
    + "."
)


def create_server() -> MCPServer:
    quiet_sdk_logging()
    server: MCPServer = MCPServer(
        name="templatize",
        title="templatize",
        instructions=INSTRUCTIONS,
        version=__version__,
        log_level="WARNING",
    )

    @server.tool(annotations=READ_ONLY)
    def suggest_parameters(
        text: Annotated[str, Field(description="The document to analyse.")],
        variants: Annotated[
            list[str] | None,
            Field(description="Optional 1-3 other versions of the same document.", max_length=3),
        ] = None,
        min_confidence: Annotated[
            float, Field(ge=0, le=1, description="Drop candidates below this confidence.")
        ] = 0.0,
        document_type: Annotated[
            str | None,
            Field(description=DOCUMENT_TYPE_HELP, max_length=64),
        ] = None,
        json_schema: Annotated[str | dict[str, Any] | None, Field(description=SCHEMA_HELP)] = None,
    ) -> tools.SuggestParametersResult:
        """Find parts of a document that should become template variables (IPs, hostnames,
        dates, emails, key/value pairs, repeated tokens, and regions that differ between
        example variants). Returns scored candidates; spans are Unicode code-point offsets."""
        with _tool_errors("suggest_parameters"):
            return tools.suggest_parameters(
                text, variants, min_confidence, document_type, _schema_text(json_schema)
            )

    @server.tool(annotations=READ_ONLY)
    def generate_template(
        text: Annotated[str, Field(description="The original document.")],
        variables: Annotated[
            list[VariableInput] | None,
            Field(
                description=(
                    "Variables to create. Identify each by `value` (exact text from the "
                    'document; occurrences: "all" or a list of 0-based indexes) or by `spans`. '
                    "Names must be lowercase snake_case. Omit to accept suggestions automatically."
                )
            ),
        ] = None,
        variants: Annotated[
            list[str] | None,
            Field(
                description="Other versions of the document (only used when `variables` is omitted).",
                max_length=3,
            ),
        ] = None,
        min_confidence: Annotated[
            float,
            Field(ge=0, le=1, description="Threshold when accepting suggestions automatically."),
        ] = 0.5,
        engine: Annotated[
            Literal["jinja2", "envsubst", "terraform"],
            Field(
                description=(
                    "jinja2 (template.j2 + .env + render.py), envsubst (${KEY} placeholders + "
                    "values.env + render.sh) or terraform (templatefile() .tftpl + "
                    "terraform.tfvars + main.tf). envsubst has no loops: lists stay text."
                )
            ),
        ] = "jinja2",
        document_type: Annotated[
            str | None,
            Field(description=DOCUMENT_TYPE_HELP, max_length=64),
        ] = None,
        json_schema: Annotated[str | dict[str, Any] | None, Field(description=SCHEMA_HELP)] = None,
    ) -> tools.GenerateTemplateResult:
        """Build a template, its values file and a script that renders it, from a document,
        and verify the round trip (template + values must reproduce the input byte for
        byte). Tell the user any `notes`."""
        with _tool_errors("generate_template"):
            return tools.generate_template(
                text,
                variables,
                variants,
                min_confidence,
                engine,
                document_type,
                _schema_text(json_schema),
            )

    @server.tool(annotations=READ_ONLY)
    def generate_helm_chart(
        text: Annotated[
            str, Field(description="Kubernetes manifests (one resource per --- document).")
        ],
        variables: Annotated[
            list[VariableInput] | None,
            Field(
                description=(
                    "Variables to create, as for generate_template, plus an optional "
                    '`values_path` (e.g. ["image", "tag"]) for values.yaml. Omit to accept '
                    "suggestions automatically (they carry Helm-style paths)."
                )
            ),
        ] = None,
        variants: Annotated[
            list[str] | None,
            Field(
                description=(
                    "The same manifests for other environments: each becomes "
                    "values-<label>.yaml with only the values that differ."
                ),
                max_length=3,
            ),
        ] = None,
        chart_name: Annotated[
            str | None, Field(description="Chart name; defaults to the first workload's name.")
        ] = None,
        variant_labels: Annotated[
            list[str] | None,
            Field(description="Labels for the variants, e.g. ['staging'].", max_length=3),
        ] = None,
        min_confidence: Annotated[
            float,
            Field(ge=0, le=1, description="Threshold when accepting suggestions automatically."),
        ] = 0.5,
        style: Annotated[
            Literal["exact", "scaffold"],
            Field(
                description=(
                    "exact: the manifests become the templates (byte-for-byte round trip). "
                    "scaffold: the standard `helm create` chart with its values.yaml filled in "
                    "from the manifests; `coverage` reports what renders identically."
                )
            ),
        ] = "exact",
    ) -> tools.GenerateHelmChartResult:
        """Build a Helm chart from Kubernetes manifests: Chart.yaml, a nested values.yaml,
        templates, and a values file per variant. Style "exact" verifies a byte-for-byte
        round trip; style "scaffold" fills in the `helm create` chart and reports coverage."""
        with _tool_errors("generate_helm_chart"):
            return tools.generate_helm_chart(
                text, variables, variants, chart_name, variant_labels, min_confidence, style
            )

    @server.tool(annotations=READ_ONLY)
    def generate_ansible_role(
        text: Annotated[str, Field(description="The document to render with Ansible.")],
        variables: Annotated[
            list[VariableInput] | None,
            Field(description="Variables, as for generate_template. Omit to accept suggestions."),
        ] = None,
        variants: Annotated[
            list[str] | None,
            Field(
                description="Other versions of the document: each becomes examples/<label>.yml.",
                max_length=3,
            ),
        ] = None,
        role_name: Annotated[
            str, Field(description="Role name (lowercase, _); also the variable prefix.")
        ] = "config",
        filename: Annotated[str, Field(description="Name of the rendered file.")] = "config",
        dest: Annotated[
            str | None, Field(description="Default destination path (<role>_dest).")
        ] = None,
        variant_labels: Annotated[
            list[str] | None, Field(description="Labels for the variants.", max_length=3)
        ] = None,
        min_confidence: Annotated[
            float,
            Field(ge=0, le=1, description="Threshold when accepting suggestions automatically."),
        ] = 0.5,
    ) -> tools.GenerateAnsibleRoleResult:
        """Package the document as an Ansible role: templates/<file>.j2, defaults/main.yml
        (variables prefixed with the role name; loops are lists), tasks/main.yml with
        byte-exact template options, a vars file per variant, and a playbook.yml."""
        with _tool_errors("generate_ansible_role"):
            return tools.generate_ansible_role(
                text, variables, variants, role_name, filename, dest, variant_labels, min_confidence
            )

    @server.tool(annotations=READ_ONLY)
    def render_template(
        template: Annotated[str, Field(description="Template source (see `engine`).")],
        values: Annotated[
            dict[str, tools.RenderValue] | None,
            Field(
                description=(
                    "Values by variable name (or .env key; case-insensitive). A loop "
                    "(`{% for user in users %}`) takes a list: of strings, or of objects "
                    '(`[{"email": "a@x", "role": "admin"}]`).'
                )
            ),
        ] = None,
        newline: Annotated[
            Literal["\n", "\r\n", "\r"] | None,
            Field(description="Line ending for template newlines; detected when omitted."),
        ] = None,
        engine: Annotated[
            Literal["jinja2", "envsubst", "terraform"],
            Field(description="The template's engine (as generated by generate_template)."),
        ] = "jinja2",
        check_schema: Annotated[
            bool,
            Field(
                description=(
                    "Also check the output against its JSON Schema: `json_schema` if given, "
                    "else the bundled one for the document (Kubernetes kinds, Compose, "
                    "GitHub Actions, GitLab CI, cloud-init, ...). Problems come back in "
                    "`schema_check`."
                )
            ),
        ] = False,
        json_schema: Annotated[str | dict[str, Any] | None, Field(description=SCHEMA_HELP)] = None,
        document_type: Annotated[
            str | None,
            Field(description=DOCUMENT_TYPE_HELP, max_length=64),
        ] = None,
    ) -> tools.RenderTemplateResult:
        """Render a template with values in a sandbox (bounded output, no file or network
        access). Render errors are returned in `error`, not raised. With `check_schema`,
        the output is checked against its JSON Schema too."""
        with _tool_errors("render_template"):
            return tools.render_template(
                template,
                values,
                newline,
                engine,
                check_schema,
                _schema_text(json_schema),
                document_type,
            )

    @server.tool(annotations=READ_ONLY)
    def validate_template(
        template: Annotated[str, Field(description="Jinja2 template source.")],
        values: Annotated[
            dict[str, tools.RenderValue] | None,
            Field(description="Values by variable name; loops take lists (see render_template)."),
        ] = None,
        expected_text: Annotated[
            str | None,
            Field(
                description="If given, check the rendered output equals this text byte for byte."
            ),
        ] = None,
    ) -> tools.ValidateTemplateResult:
        """Check a template: syntax, which variables it references, missing/unused values,
        whether it renders, and optionally whether it reproduces `expected_text` exactly."""
        with _tool_errors("validate_template"):
            return tools.validate_template(template, values, expected_text)

    return server


def transport_security(allowed_hosts: list[str]) -> TransportSecuritySettings:
    """DNS-rebinding protection: only accept these Host headers (and matching Origins)."""
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=[f"http://{h}" for h in allowed_hosts]
        + [f"https://{h}" for h in allowed_hosts],
    )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="templatize-mcp", description="templatize MCP server")
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1", help="http transport only")
    ap.add_argument("--port", type=int, default=8001, help="http transport only")
    ap.add_argument(
        "--allowed-host",
        action="append",
        default=None,
        help="extra Host header to accept over http (repeatable), e.g. myhost.ts.net:*",
    )
    args = ap.parse_args(argv)
    # stdout carries the protocol in stdio mode: logs must go to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    server = create_server()
    if args.transport == "stdio":
        server.run("stdio")
    else:
        hosts = DEFAULT_ALLOWED_HOSTS + (args.allowed_host or [])
        server.run(
            "streamable-http",
            host=args.host,
            port=args.port,
            streamable_http_path="/mcp",
            stateless_http=True,
            json_response=True,
            transport_security=transport_security(hosts),
        )


if __name__ == "__main__":
    main()
