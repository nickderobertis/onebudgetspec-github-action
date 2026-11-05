//! The driver under test: an installed binary when one is named, else this profile's own build.
use std::path::{Path, PathBuf};

/// The executable to drive.
pub fn binary() -> PathBuf {
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
