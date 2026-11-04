//! Exercise the repository tooling as public subprocess interfaces.
#[test]
fn release_and_workflow_contracts() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = std::process::Command::new("python3")
        .arg("crates/repo-checks/repository_tests.py")
        .current_dir(root)
        .output()
        .expect("Python test runner");
    assert!(
        output.status.success(),
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}
