import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ELECTRON_ROOT = REPO_ROOT / "electron-app"
COMPONENTS = ELECTRON_ROOT / "src" / "components"


def test_shell_exposes_only_the_intended_product_surfaces():
    """The shell exposes the observer, hierarchy, graph, search, and assistant.

    Graph is a read-only view of the same entities the hierarchy edits, so it
    is a peer of those surfaces rather than a mode inside one. The point of
    this test is that the list does not quietly grow again.
    """
    app_source = (ELECTRON_ROOT / "src" / "App.tsx").read_text()
    assert all(
        (COMPONENTS / name).exists()
        for name in ("RecordView.tsx", "HierarchyView.tsx", "SearchView.tsx")
    )
    assert (COMPONENTS / "GraphView.tsx").exists()
    assert "type View = 'record' | 'hierarchy' | 'graph' | 'search' | 'assistant'" in app_source
    assert "Sidebar" not in app_source


def test_scheduled_legacy_views_are_deleted():
    """The legacy JSX views stay gone.

    `GraphView` is deliberately absent from this list. The Cytoscape original
    was deleted with the rest of them; what carries the name now is a
    dependency-free TypeScript reimplementation — the assertion below that the
    graph libraries are still uninstalled is what actually holds that line.
    """
    removed_stems = {
        "BrowseView",
        "ConflictResolver",
        "EntityDetails",
        "GoalCompiler",
        "GoalMeansSwitchboard",
        "GoalReviewView",
        "GoalWorkbench",
        "GoalsDashboard",
        "GraphExplorer",
        "HeatmapView",
        "IdentityCompiler",
        "PossibleSelvesNavigator",
        "RecordingOnlyScreen",
        "ReframeView",
        "ReviewView",
        "SetupWizard",
        "Sidebar",
    }
    assert not [path for path in COMPONENTS.iterdir() if path.stem in removed_stems]

    dependencies = json.loads((ELECTRON_ROOT / "package.json").read_text())["dependencies"]
    graph_dependencies = {
        "cytoscape",
        "cytoscape-dagre",
        "cytoscape-elk",
        "dagre",
        "elkjs",
        "graphology",
        "graphology-layout",
        "graphology-layout-forceatlas2",
        "graphology-types",
        "sigma",
    }
    assert graph_dependencies.isdisjoint(dependencies)


def test_new_core_is_browser_safe_and_runtime_validated():
    core_paths = (
        "App.tsx",
        "api/client.ts",
        "api/contracts.ts",
        "components/RecordView.tsx",
        "components/ExclusionSettingsPanel.tsx",
        "components/HierarchyView.tsx",
        "components/GraphView.tsx",
        "components/SearchView.tsx",
    )
    combined = "\n".join(
        (ELECTRON_ROOT / "src" / path).read_text() for path in core_paths
    )
    assert "window.electron" not in combined
    assert "safeParse" in combined
    assert "from 'zod'" in combined
    assert "Promise<any>" not in combined
    assert (ELECTRON_ROOT / "src" / "platform" / "shell.ts").exists()


def test_typed_client_matches_retained_server_contract():
    client = (ELECTRON_ROOT / "src" / "api" / "client.ts").read_text()
    required_paths = {
        "/api/start",
        "/api/stop",
        "/api/status",
        "/api/diagnostics/observer",
        "/api/config/env",
        "/api/apps/installed",
        "/api/settings/exclusions",
        "/api/hierarchy",
        "/api/graph/search",
        "/api/graph/timeline",
    }
    assert all(path in client for path in required_paths)
    # `/api/onboarding` is deliberately absent from this list. The study's
    # version was removed with the rest of the field-study instrumentation;
    # what exists now is a local, optional self-description with no participant
    # ID, no email, and no remote storage, kept because the inference stages
    # read it as context.
    removed_fragments = (
        "/api/experiments",
        "/api/coach",
        "/api/study",
        "/api/review",
        "/api/goals/heatmap",
        "/api/goals/possible-selves",
    )
    assert all(fragment not in client for fragment in removed_fragments)


def test_websocket_owner_cleans_up_socket_and_reconnect_timer():
    client = (ELECTRON_ROOT / "src" / "api" / "client.ts").read_text()
    assert "this.listeners.size === 0" in client
    assert "clearTimeout(this.reconnectTimer)" in client
    assert "this.socket = null" in client


def test_phase6b_hierarchy_editor_covers_every_transactional_mutation():
    """Every transactional mutation stays reachable, and stays revision-checked.

    The drag-to-reparent outline that used to carry these is gone: Hierarchy is
    now Cards and Table, and the review-then-compile workflow is the editing
    surface. The mutations themselves did not go anywhere — the typed client
    still covers each one, still sends the revision the caller read, and the
    server side is exercised in test_hierarchy_api.py. This test holds that
    line so the endpoints cannot quietly drift out of the client.
    """
    client = (ELECTRON_ROOT / "src" / "api" / "client.ts").read_text()
    contracts = (ELECTRON_ROOT / "src" / "api" / "contracts.ts").read_text()
    editor = (COMPONENTS / "hierarchy" / "CardEditor.tsx").read_text()

    assert all(
        method in client
        for method in (
            "updateEntity(",
            "reparentEntity(",
            "mergeEntities(",
            "splitEntity(",
            "deleteEntity(",
        )
    )
    assert "expected_revision" in contracts
    assert "child_ids" in contracts
    assert "reparent_children_to" in contracts

    # The card editor stages edits and commits them in one compile, so its
    # conflict handling and refresh live there rather than per-mutation.
    assert "api.compileHierarchy" in editor
    assert "TempoApiError" in editor
    assert "onCompiled" in editor


def test_frontend_toolchain_is_pinned_and_checks_both_typescript_targets():
    package = json.loads((ELECTRON_ROOT / "package.json").read_text())
    assert package["packageManager"].startswith("pnpm@11.22.0+")
    # pnpm 11.22.0 refuses to run on Node < 22.13, so the declared engine floor
    # must not admit a Node version that cannot run the pinned package manager.
    assert package["engines"]["node"] == ">=22.13 <25"
    assert package["scripts"]["typecheck"] == (
        "tsc -p tsconfig.json --noEmit && tsc -p tsconfig.node.json --noEmit"
    )
    assert package["devDependencies"]["@types/react"].startswith("^18.")
    assert package["devDependencies"]["@types/react-dom"].startswith("^18.")

    for name in ("tsconfig.json", "tsconfig.node.json"):
        config = json.loads((ELECTRON_ROOT / name).read_text())
        assert config["compilerOptions"]["allowJs"] is False
        assert config["compilerOptions"]["strict"] is True
        assert not {
            "noImplicitAny",
            "strictNullChecks",
            "strictFunctionTypes",
            "strictBindCallApply",
            "strictPropertyInitialization",
            "noImplicitThis",
            "alwaysStrict",
            "useUnknownInCatchVariables",
        }.intersection(config["compilerOptions"])


# Comments and string literals are prose, not types: "click any card" is not an
# `any` annotation. Strip them before looking for escape hatches so the ban
# stays strict about code without policing the English word.
_COMMENTS_AND_STRINGS = re.compile(
    r"""
      /\*.*?\*/            # block comment
    | //[^\n]*             # line comment
    | '(?:\\.|[^'\\])*'    # single-quoted string
    | "(?:\\.|[^"\\])*"    # double-quoted string
    | `(?:\\.|[^`\\])*`    # template literal
    """,
    re.DOTALL | re.VERBOSE,
)


def _code_only(source: str) -> str:
    """Blank out comments and string literals, preserving line structure."""
    return _COMMENTS_AND_STRINGS.sub(
        lambda match: "\n" * match.group(0).count("\n"), source
    )


def test_phase6c_types_accessibility_and_responsive_contract():
    typed_sources = [
        *ELECTRON_ROOT.glob("*.ts"),
        *(ELECTRON_ROOT / "shared").rglob("*.ts"),
        *(ELECTRON_ROOT / "src").rglob("*.ts"),
        *(ELECTRON_ROOT / "src").rglob("*.tsx"),
    ]
    # `any` is only meaningful in code, while a suppression pragma only ever
    # lives in a comment — so each is checked against the text it can appear in.
    explicit_any = re.compile(r"\bany\b")
    suppression = re.compile(r"@ts-(?:ignore|expect-error)")

    offenders: dict[str, list[str]] = {}
    for path in typed_sources:
        source = path.read_text()
        found = explicit_any.findall(_code_only(source)) + suppression.findall(source)
        if found:
            offenders[str(path.relative_to(REPO_ROOT))] = found
    assert not offenders

    modal = (COMPONENTS / "Modal.tsx").read_text()
    hierarchy = (COMPONENTS / "HierarchyView.tsx").read_text()
    styles = (ELECTRON_ROOT / "src" / "styles" / "main.css").read_text()
    assert 'role="dialog"' in modal
    assert 'aria-modal="true"' in modal
    assert "previouslyFocused?.focus()" in modal
    assert "e.key !== 'Tab'" in modal
    # Hierarchy is Cards and Table now, not a tree widget: the accessibility
    # contract it owns is the mode switch, and the cards' own expand state.
    assert 'role="group"' in hierarchy
    assert 'aria-pressed=' in hierarchy
    assert 'aria-expanded=' in (COMPONENTS / "hierarchy" / "Cards.tsx").read_text()
    assert "@media (max-width: 760px)" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles
    assert ":focus-visible" in styles


def test_phase7_python_ui_build_is_reproducible_and_same_origin():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    build_script = (REPO_ROOT / "scripts" / "build-wheel.sh").read_text()
    ui_build_script = (REPO_ROOT / "scripts" / "build-ui.sh").read_text()
    python_server_build = (REPO_ROOT / "scripts" / "build-python-server.sh").read_text()
    vite = (ELECTRON_ROOT / "vite.config.ts").read_text()
    shell = (ELECTRON_ROOT / "src" / "platform" / "shell.ts").read_text()
    electron_main = (ELECTRON_ROOT / "main.ts").read_text()

    assert '"tempo.ui" = ["**/*"]' in pyproject
    assert "build-ui.sh" in build_script
    assert "build-ui.sh" in python_server_build
    assert "install --frozen-lockfile" in ui_build_script
    assert '"${PNPM_BIN}" typecheck' in ui_build_script
    assert '"${PNPM_BIN}" build:python-ui' in ui_build_script
    assert '"${PYTHON_BIN}" -m build' in build_script
    assert "TEMPO_BUILD_TARGET === 'python'" in vite
    assert "dist-python" in vite
    assert "window.location.origin" in shell
    assert "setLoginItemSettings" in electron_main
    assert (REPO_ROOT / "tempo" / "ui" / "__init__.py").exists()


def test_single_production_pipeline_surface():
    tempo_root = REPO_ROOT / "tempo"
    assert (tempo_root / "pipelines" / "tempo_pipeline.py").exists()
    assert not (tempo_root / "pipelines" / "ablation_condition.py").exists()
    assert not list((tempo_root / "experiments").glob("*.py"))
    assert not (tempo_root / "coach.py").exists()

    orchestrator_source = (tempo_root / "pipelines" / "orchestrator.py").read_text()
    server_source = (tempo_root / "server.py").read_text()
    assert "CONDITION_DEFS" not in orchestrator_source
    assert "self.conditions" not in orchestrator_source
    assert '"/api/experiments' not in server_source
    assert '"/api/coach' not in server_source
