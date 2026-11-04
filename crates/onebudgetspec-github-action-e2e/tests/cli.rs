//! Real compiled command journeys, isolated from the checkout.
use std::path::{Path, PathBuf};

fn binary() -> PathBuf {
    static BUILT: std::sync::OnceLock<PathBuf> = std::sync::OnceLock::new();
    if let Some(path) = std::env::var_os("SCAFFOLD_BINARY") {
        return path.into();
    }
    let path = BUILT.get_or_init(|| {
        let exe = std::env::current_exe().expect("the test executable's path");
        let profile_dir = exe
            .parent()
            .and_then(Path::parent)
            .expect("the test executable sits in <target>/<profile>/deps");
        let path = profile_dir.join(format!(
            "onebudgetspec-github-action{}",
            std::env::consts::EXE_SUFFIX
        ));
        {
            let target_dir = profile_dir.parent().expect("<target>/<profile>");
            let mut build = std::process::Command::new(env!("CARGO"));
            build
                .args([
                    "build",
                    "--quiet",
                    "--locked",
                    "-p",
                    "onebudgetspec-github-action",
                ])
                .arg("--target-dir")
                .arg(target_dir);
            if profile_dir
                .file_name()
                .is_some_and(|name| name == "release")
            {
                build.arg("--release");
            }
            let status = build.status().expect("cargo spawns");
            assert!(
                status.success() && path.is_file(),
                "building {} failed ({status}); run `cargo build -p onebudgetspec-github-action` \
                 to see why, then re-run",
                path.display()
            );
        }
        path
    });
    path.clone()
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
