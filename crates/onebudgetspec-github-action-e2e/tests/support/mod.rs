//! The driver under test: an installed binary when one is named, else this profile's own build.
use std::path::{Path, PathBuf};

/// The executable to drive.
pub fn binary() -> PathBuf {
    static BUILT: std::sync::OnceLock<(PathBuf, std::fs::File)> = std::sync::OnceLock::new();
    if let Some(path) = std::env::var_os("SCAFFOLD_BINARY") {
        return path.into();
    }
    let (path, _) = BUILT.get_or_init(|| {
        let exe = std::env::current_exe().expect("the test executable's path");
        let profile_dir = exe
            .parent()
            .and_then(Path::parent)
            .expect("the test executable sits in <target>/<profile>/deps");
        let path = profile_dir.join(format!(
            "onebudgetspec-github-action{}",
            std::env::consts::EXE_SUFFIX
        ));
        // On macOS cargo copies this binary out of deps/, so even an up-to-date build removes
        // and re-copies it. A sibling test process's build must not run while this one spawns
        // it: build alone, then keep a shared lock until this process exits.
        let lock = std::fs::File::create(profile_dir.join("e2e-driver.lock"))
            .expect("the driver's build lock is creatable under the target directory");
        lock.lock().expect("the driver's build lock is takeable");
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
        lock.lock_shared()
            .expect("the driver's build lock can be shared");
        (path, lock)
    });
    path.clone()
}
