//! The action's real entry point, end to end; the journeys are `action_journeys.py`.
//! Unix only: the action runs on Linux and macOS, and Windows runners get a diagnostic.
#![cfg(unix)]
mod support;

#[test]
fn action_journeys() {
    let crate_directory = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    let output = std::process::Command::new("uv")
        .args([
            "run",
            "--no-project",
            "--quiet",
            "--with",
            "pyyaml",
            "python",
        ])
        .arg(crate_directory.join("action_journeys.py"))
        .env("SCAFFOLD_BINARY", support::binary())
        .current_dir(crate_directory)
        .output()
        .expect("uv runs the journeys; install it with `just bootstrap`");
    assert!(
        output.status.success(),
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}
