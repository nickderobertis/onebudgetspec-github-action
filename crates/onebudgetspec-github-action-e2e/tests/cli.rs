//! Real compiled command journeys, isolated from the checkout.
use std::path::{Path, PathBuf};

fn binary() -> PathBuf {
    if let Some(path) = std::env::var_os("SCAFFOLD_BINARY") {
        return path.into();
    }
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let target =
        std::env::var_os("CARGO_TARGET_DIR").map_or_else(|| root.join("target"), PathBuf::from);
    target.join("debug").join(format!(
        "onebudgetspec-github-action{}",
        std::env::consts::EXE_SUFFIX
    ))
}

#[test]
fn help_version_and_rejection_perform_no_work() {
    let dir = tempfile::tempdir().expect("isolated working directory");
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let metadata: serde_json::Value = serde_json::from_slice(
        &std::process::Command::new("cargo")
            .args(["metadata", "--no-deps", "--format-version", "1", "--locked"])
            .current_dir(root)
            .output()
            .expect("cargo metadata")
            .stdout,
    )
    .expect("metadata JSON");
    let version = metadata["packages"]
        .as_array()
        .expect("packages")
        .iter()
        .find(|p| p["name"] == "onebudgetspec-github-action")
        .expect("package")["version"]
        .as_str()
        .expect("version");
    for args in [vec![], vec!["--help"], vec!["-h"]] {
        let mut command = assert_cmd::Command::new(binary());
        command
            .current_dir(dir.path())
            .args(args)
            .assert()
            .success()
            .stdout(predicates::str::contains(
                "Usage: onebudgetspec-github-action",
            ));
    }
    for flag in ["--version", "-V"] {
        let mut command = assert_cmd::Command::new(binary());
        command
            .current_dir(dir.path())
            .arg(flag)
            .assert()
            .success()
            .stdout(format!("onebudgetspec-github-action {version}\n"));
    }
    for args in [
        vec!["--unsupported"],
        vec!["--help", "--unsupported"],
        vec!["report"],
    ] {
        let mut command = assert_cmd::Command::new(binary());
        command
            .current_dir(dir.path())
            .args(args)
            .assert()
            .code(2)
            .stdout("")
            .stderr(predicates::str::contains("error: unsupported arguments"));
    }
    assert_eq!(std::fs::read_dir(dir.path()).expect("directory").count(), 0);
}
