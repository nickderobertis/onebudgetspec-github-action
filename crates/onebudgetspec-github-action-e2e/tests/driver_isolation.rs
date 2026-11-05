//! The e2e driver survives a sibling task building in the same target directory. Real cargo runs
//! that build; between its runs the uplifted binary is swapped for a copy, because on macOS cargo
//! copies it, so an up-to-date build removes and rewrites it rather than leaving it alone. Nothing
//! here touches the real target directory, which sibling tasks are using: a copy of this test
//! binary runs from a scratch one, so `support::binary` resolves against that.
#![cfg(unix)]
mod support;

use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::time::{Duration, Instant};

const PROBE_MODE: &str = "E2E_DRIVER_ISOLATION_PROBE";
const PROBES: usize = 4;
const SPAWNS_PER_PROBE: usize = 200;
const DEADLINE: Duration = Duration::from_secs(300);
const ABSENT_FOR: Duration = Duration::from_millis(100);
const TEST_NAME: &str = "a_sibling_build_in_the_same_target_never_takes_the_driver_away";

/// This toolchain and a workspace to build, with no coverage flags or target override from the run.
fn clean(mut command: Command, scratch: &Path) -> Command {
    for (key, _) in std::env::vars_os() {
        let key = key.to_string_lossy();
        if [
            "CARGO",
            "__CARGO",
            "LLVM_",
            "RUST_",
            "RUSTC",
            "RUSTDOC",
            "RUSTFLAGS",
            "SCAFFOLD_",
        ]
        .iter()
        .any(|prefix| key.starts_with(prefix))
        {
            command.env_remove(key.as_ref());
        }
    }
    // An instrumented copy of this binary would otherwise write its profile into the checkout.
    command
        .env("LLVM_PROFILE_FILE", scratch.join("probe-%p.profraw"))
        .current_dir(env!("CARGO_MANIFEST_DIR"));
    command
}

fn sibling_build(scratch: &Path) {
    let mut build = clean(Command::new(env!("CARGO")), scratch);
    let output = build
        .args([
            "build",
            "--quiet",
            "--locked",
            "-p",
            "onebudgetspec-github-action",
        ])
        .arg("--target-dir")
        .arg(scratch)
        .output()
        .expect("cargo spawns");
    assert!(
        output.status.success(),
        "the sibling build failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn start_probe(executable: &Path, scratch: &Path, index: usize) -> Child {
    let log = |stream: &str| {
        std::fs::File::create(scratch.join(format!("probe-{index}.{stream}"))).expect("probe log")
    };
    clean(Command::new(executable), scratch)
        .args(["--exact", TEST_NAME, "--nocapture"])
        .env(PROBE_MODE, "1")
        .stdin(Stdio::null())
        .stdout(log("out"))
        .stderr(log("err"))
        .spawn()
        .expect("the copied test binary spawns")
}

fn wait_within_deadline(mut probe: Child, started: Instant) -> bool {
    loop {
        if let Some(status) = probe.try_wait().expect("the probe can be waited on") {
            return status.success();
        }
        if started.elapsed() > DEADLINE {
            let _ = probe.kill();
            let _ = probe.wait();
            return false;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

#[test]
fn a_sibling_build_in_the_same_target_never_takes_the_driver_away() {
    if std::env::var_os(PROBE_MODE).is_some() {
        for _ in 0..SPAWNS_PER_PROBE {
            let output = Command::new(support::binary())
                .arg("--version")
                .output()
                .expect("the driver is there to spawn");
            assert!(output.status.success(), "the driver failed: {output:?}");
        }
        return;
    }
    let started = Instant::now();
    let scratch = tempfile::tempdir().expect("scratch target directory");
    let scratch = scratch.path();
    let deps = scratch.join("debug/deps");
    std::fs::create_dir_all(&deps).expect("scratch deps directory");
    let executable = deps.join("driver-isolation-probe");
    std::fs::copy(
        std::env::current_exe().expect("this test binary's path"),
        &executable,
    )
    .expect("copy this test binary into the scratch target");

    sibling_build(scratch);
    let uplifted = scratch.join("debug/onebudgetspec-github-action");
    let original = scratch.join("sibling-original");
    std::fs::copy(&uplifted, &original).expect("keep the sibling's binary to copy back");

    let stop = AtomicBool::new(false);
    let rewrites = AtomicUsize::new(0);
    let passed = std::thread::scope(|scope| {
        let competing = scope.spawn(|| {
            while !stop.load(Ordering::Relaxed) {
                sibling_build(scratch);
                std::fs::remove_file(&uplifted).expect("the sibling's binary is there");
                // Long enough for a spawn to land here, not only on the half-written copy after.
                std::thread::sleep(ABSENT_FOR);
                // Never copied over a link another build made meanwhile: that would rewrite deps/.
                let _ = std::fs::remove_file(&uplifted);
                std::fs::copy(&original, &uplifted).expect("copy the sibling's binary back");
                rewrites.fetch_add(1, Ordering::Relaxed);
            }
        });
        let probes: Vec<Child> = (0..PROBES)
            .map(|index| start_probe(&executable, scratch, index))
            .collect();
        let passed: Vec<bool> = probes
            .into_iter()
            .map(|probe| wait_within_deadline(probe, started))
            .collect();
        stop.store(true, Ordering::Relaxed);
        competing.join().expect("the competing build finishes");
        passed
    });

    let logs: String = (0..PROBES)
        .flat_map(|index| ["out", "err"].map(|stream| (index, stream)))
        .map(|(index, stream)| {
            std::fs::read_to_string(scratch.join(format!("probe-{index}.{stream}")))
                .expect("probe log")
        })
        .collect();
    assert!(
        passed.iter().all(|passed| *passed) && !logs.contains("NotFound"),
        "a probe failed or passed the {DEADLINE:?} deadline: {logs}"
    );
    assert_eq!(
        logs.matches("test result: ok. 1 passed").count(),
        PROBES,
        "every probe really ran: {logs}"
    );
    assert!(
        rewrites.load(Ordering::Relaxed) > 0,
        "the competing build never rewrote the shared binary, so this proved nothing"
    );
}
