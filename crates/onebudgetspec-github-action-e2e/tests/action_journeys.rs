//! The action's real entry point, end to end; the journeys are `action_journeys.py`.
//! Unix only: the action runs on Linux and macOS, and Windows runners get a diagnostic.
#![cfg(unix)]
mod support;

use onebudgetspec_core::comparison::ComparisonReport;
use onebudgetspec_core::render_markdown;

#[test]
fn action_journeys() {
    let crate_directory = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    // The journeys leave each saved report beside its saved Markdown here, for the check below.
    let rendered = tempfile::tempdir().expect("a directory for the saved reports");
    let output = std::process::Command::new("uv")
        .args([
            "run",
            "--no-project",
            "--quiet",
            "--with",
            "pyyaml",
            "--with",
            "jsonschema",
            "python",
        ])
        .arg(crate_directory.join("action_journeys.py"))
        .env("SCAFFOLD_BINARY", support::binary())
        .env("JOURNEY_RENDERED", rendered.path())
        .current_dir(crate_directory)
        .output()
        .expect("uv runs the journeys; install it with `just bootstrap`");
    assert!(
        output.status.success(),
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert_saved_markdown_is_the_core_render(rendered.path());
}

/// Each job's saved `report.md` is, byte for byte, the released core's render of the
/// `comparison-report.json` saved beside it: nothing rewrites the Markdown afterwards.
fn assert_saved_markdown_is_the_core_render(rendered: &std::path::Path) {
    let mut jobs = 0;
    for pair in std::fs::read_dir(rendered).expect("the saved reports' directory") {
        let pair = pair.expect("a saved report's directory").path();
        let saved = std::fs::read(pair.join("comparison-report.json")).expect("the saved report");
        let report: ComparisonReport =
            serde_json::from_slice(&saved).expect("the saved report reads as the core's type");
        let markdown = std::fs::read_to_string(pair.join("report.md")).expect("the saved Markdown");
        assert_eq!(render_markdown(&report), markdown, "{}", pair.display());
        jobs += 1;
    }
    assert!(
        jobs > 0,
        "the fixture repository's journey saved no report to compare"
    );
}
