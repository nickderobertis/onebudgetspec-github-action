//! Real compiled command journeys, isolated from the checkout.
use std::path::Path;

mod support;
use support::binary;

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

#[cfg(unix)]
#[test]
fn output_errors_fail_without_panicking() {
    use std::os::fd::OwnedFd;
    use std::os::unix::net::UnixStream;
    use std::process::{Command, Stdio};
    // A closed peer fails writes on both Linux and macOS, unlike /dev/full.
    // The child still runs the real instrumented binary in this test's profile.
    let broken_output = || {
        let (writer, reader) = UnixStream::pair().expect("output socket pair");
        drop(reader);
        Stdio::from(OwnedFd::from(writer))
    };
    for args in [vec!["--help"], vec!["--version"]] {
        let output = Command::new(binary())
            .args(args)
            .stdout(broken_output())
            .stderr(Stdio::piped())
            .output()
            .expect("real binary");
        assert_eq!(output.status.code(), Some(1));
        assert!(String::from_utf8_lossy(&output.stderr).contains("error: output failed:"));
    }
    let status = Command::new(binary())
        .arg("--unsupported")
        .stderr(broken_output())
        .status()
        .expect("real binary");
    assert_eq!(status.code(), Some(1));
}
