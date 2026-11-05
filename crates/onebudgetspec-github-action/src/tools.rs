//! The executables a run drives, each found beside this one first.
use std::ffi::{OsStr, OsString};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use crate::state::Failure;

/// The core command line.
pub const CORE: &str = "onebudgetspec";
/// The onevcs transport plugin.
pub const PLUGIN: &str = "onebudgetspec-plugin-onevcs";
/// This tool, as a budget command names it.
pub const DRIVER: &str = "onebudgetspec-github-action";

/// Directory holding this executable, whichever name it was invoked under.
fn own_directory() -> Option<PathBuf> {
    std::env::current_exe()
        .ok()?
        .parent()
        .map(Path::to_path_buf)
}

/// Resolve a bare program against `path`, as a shell would.
pub fn on_path(program: &str, path: &OsStr) -> Option<PathBuf> {
    std::env::split_paths(path)
        .map(|directory| directory.join(format!("{program}{}", std::env::consts::EXE_SUFFIX)))
        .find(|candidate| candidate.is_file())
}

/// A tool beside this executable, else on `PATH`.
pub fn locate(program: &str) -> Result<PathBuf, Failure> {
    let beside = own_directory()
        .map(|directory| directory.join(format!("{program}{}", std::env::consts::EXE_SUFFIX)))
        .filter(|candidate| candidate.is_file());
    beside
        .or_else(|| on_path(program, &std::env::var_os("PATH").unwrap_or_default()))
        .ok_or_else(|| {
            Failure::output(format!(
                "{program} is not installed; install it beside this tool or on PATH"
            ))
        })
}

/// The search path budget commands run with: the proxy first, this tool and its siblings next.
pub fn search_path(proxy_directory: &Path) -> OsString {
    let mut entries = vec![proxy_directory.to_path_buf()];
    entries.extend(own_directory());
    entries.extend(std::env::split_paths(
        &std::env::var_os("PATH").unwrap_or_default(),
    ));
    std::env::join_paths(entries).unwrap_or_default()
}

/// Run a tool to completion, keeping both of its streams.
pub fn capture(command: &mut Command, what: &str) -> Result<Output, Failure> {
    command
        .output()
        .map_err(|error| Failure::output(format!("cannot run {what}: {error}")))
}
