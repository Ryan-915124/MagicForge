import json
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "frontend" / "scripts" / "localization-inventory.mjs"


def run_inventory(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", str(SCRIPT_PATH), *arguments],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def run_module(program: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "--input-type=module", "--eval", program],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_inventory_resolves_current_translator_refs_and_mapping_keys() -> None:
    completed = run_inventory()
    assert completed.returncode == 0, completed.stderr or completed.stdout
    inventory = json.loads(completed.stdout)

    assert inventory["roots"] == {
        "project": ".",
        "frontend": "frontend",
        "source": "frontend/src",
        "messages": "frontend/src/lib/i18n/messages.ts",
    }
    assert inventory["summary"]["unusedKeys"] == 0
    assert inventory["summary"]["dynamicTranslationCalls"] == 0

    used_keys = set(inventory["usage"]["usedKeys"])
    assert {
        "chat.session.requestFailed",
        "evidence.browser.retrievedPlural",
        "evidence.browser.retrievedSingle",
        "research.governance.productionTouched",
        "research.governance.productionUntouched",
        "research.metric.queriesExecuted",
        "research.stage.memory.description",
    } <= used_keys


def test_inventory_finds_visible_badge_strings_in_jsx_expressions() -> None:
    completed = run_inventory()
    assert completed.returncode == 0, completed.stderr or completed.stdout
    inventory = json.loads(completed.stdout)
    findings = {
        (finding["file"], finding["kind"], finding["value"])
        for finding in inventory["hardcodedCandidates"]
    }

    assert (
        "frontend/src/components/shared/confidence-badge.tsx",
        "jsx_expression",
        "Unassessed",
    ) in findings
    for value in ("Scientific", "Expert practice", "Interpretation", "Unclassified"):
        assert (
            "frontend/src/components/shared/origin-badge.tsx",
            "jsx_expression",
            value,
        ) in findings


def test_scan_source_handles_translator_aliases_refs_conditions_and_maps() -> None:
    source = """
const keys = { ready: "alpha.ready", failed: "alpha.failed" } as const
const labels = { a: "Visible A", b: "Visible B" } as const
function Demo({ ok }: { ok: boolean }) {
  const { t: translate } = useLocale()
  const translateRef = useRef(translate)
  const tr = translateRef.current
  return <div title={ok ? "Title A" : `Title B`}>
    {ok ? labels.a : labels["b"]}
    {translate(ok ? keys.ready : keys.failed)}
    {translateRef.current("ref.key")}
    {tr("alias.key")}
  </div>
}
"""
    program = f"""
import {{ createSourceFile, scanSource }} from {json.dumps(SCRIPT_PATH.as_uri())}
const source = {json.dumps(source)}
const sourceFile = createSourceFile({json.dumps(str(PROJECT_ROOT / 'frontend/src/fixture.tsx'))}, source)
process.stdout.write(JSON.stringify(scanSource(sourceFile)))
"""
    completed = run_module(program)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)

    assert {call["key"] for call in result["calls"]} == {
        "alpha.failed",
        "alpha.ready",
        "alias.key",
        "ref.key",
    }
    assert result["dynamicCalls"] == []
    assert {finding["value"] for finding in result["hardcodedCandidates"]} == {
        "Title A",
        "Title B",
        "Visible A",
        "Visible B",
    }


def test_catalog_rejects_empty_and_whitespace_only_values() -> None:
    program = f"""
import {{ createSourceFile, extractCatalog }} from {json.dumps(SCRIPT_PATH.as_uri())}
const failures = []
for (const value of ["", " \\t\\n "]) {{
  const sourceFile = createSourceFile(
    {json.dumps(str(PROJECT_ROOT / 'frontend/src/catalog.ts'))},
    `const enUS = {{ key: ${{JSON.stringify(value)}} }}`,
  )
  try {{
    extractCatalog(sourceFile, "enUS")
  }} catch (error) {{
    failures.push(error.message)
  }}
}}
process.stdout.write(JSON.stringify(failures))
"""
    completed = run_module(program)
    assert completed.returncode == 0, completed.stderr
    failures = json.loads(completed.stdout)
    assert len(failures) == 2
    assert all("must not be empty or whitespace-only" in failure for failure in failures)


def test_cli_help_unknown_arguments_and_codepoint_sort() -> None:
    help_result = run_inventory("--help")
    assert help_result.returncode == 0
    assert help_result.stdout.startswith("Usage:")
    assert help_result.stderr == ""

    unknown_result = run_inventory("--unknown")
    assert unknown_result.returncode == 2
    assert unknown_result.stdout == ""
    assert "unknown argument: --unknown" in unknown_result.stderr

    program = f"""
import {{ compareCodePoints }} from {json.dumps(SCRIPT_PATH.as_uri())}
process.stdout.write(JSON.stringify(["𐀀", "�"].sort(compareCodePoints)))
"""
    sort_result = run_module(program)
    assert sort_result.returncode == 0, sort_result.stderr
    assert json.loads(sort_result.stdout) == ["�", "𐀀"]
