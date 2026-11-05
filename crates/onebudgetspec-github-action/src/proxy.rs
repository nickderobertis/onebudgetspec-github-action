//! Every GitHub API request of a run is logged here, so its total is counted, not estimated.
//!
//! A run points `ONEVCS_GH` and `PATH`'s `gh` at this executable under the name `gh`. Budget
//! commands and the plugin then reach GitHub exactly as they would have, one bounded retry
//! honouring `Retry-After` happens here, and each exchange is logged by phase and budget.
//! `gh` itself says what it sent: under `GH_DEBUG=1` it writes one `* Request to URL` line per
//! HTTP exchange, without any body, whichever verb made it and however many pages it took.
use std::ffi::OsString;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Output};
use std::time::Duration;

use serde_json::{Value, json};

use crate::state::Failure;

/// The real `gh`.
const REAL: &str = "BUDGET_PROXY_REAL_GH";
/// The log every call appends to.
const LOG: &str = "BUDGET_PROXY_LOG";
/// Which part of the run is calling.
const PHASE: &str = "BUDGET_PROXY_PHASE";
/// Set when budget commands get no GitHub credential in this run.
const DENY: &str = "BUDGET_PROXY_DENY";
/// A `Retry-After` longer than this is not waited for, so one job cannot be parked.
const MAX_RETRY_WAIT_SECONDS: u64 = 60;
/// Retries one whole run may spend, so a limited host is not pressed further.
const MAX_RETRIES_PER_RUN: usize = 2;

/// What `gh` writes per HTTP exchange under `GH_DEBUG`, and every line of that kind.
const REQUEST_LINE: &str = "* Request to ";
const GH_DEBUG_LINES: &str = "* Request ";

/// How one call ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Outcome {
    /// The host answered with success.
    Answered,
    /// The host refused it for a rate limit.
    RateLimited,
    /// The host refused the credential.
    Unauthorized,
    /// The host answered anything else, or could not be reached.
    Failed,
    /// This run gives the caller no credential, so no request was made.
    NoCredential,
    /// This runner has no GitHub CLI, so no request was made.
    NoGh,
}

impl Outcome {
    const ALL: [Self; 6] = [
        Self::Answered,
        Self::RateLimited,
        Self::Unauthorized,
        Self::Failed,
        Self::NoCredential,
        Self::NoGh,
    ];

    /// The spelling the log records.
    const fn as_str(self) -> &'static str {
        match self {
            Self::Answered => "answered",
            Self::RateLimited => "rate-limited",
            Self::Unauthorized => "unauthorized",
            Self::Failed => "failed",
            Self::NoCredential => "no-credential",
            Self::NoGh => "no-gh",
        }
    }

    /// Back from the log's spelling.
    fn parse(spelling: &str) -> Option<Self> {
        Self::ALL
            .into_iter()
            .find(|outcome| outcome.as_str() == spelling)
    }
}

/// Whether this process was started as the proxy rather than as the tool.
pub fn invoked_as_gh() -> bool {
    std::env::args_os().next().is_some_and(|name| {
        Path::new(&name)
            .file_stem()
            .is_some_and(|stem| stem == "gh")
    })
}

/// Make `directory` hold the proxy under the name `gh`.
pub fn install(directory: &Path) -> Result<PathBuf, Failure> {
    let failed = |error: &dyn std::fmt::Display| {
        Failure::output(format!(
            "cannot prepare the request counter in {}: {error}; choose a writable --out",
            directory.display()
        ))
    };
    std::fs::create_dir_all(directory).map_err(|error| failed(&error))?;
    let this = std::env::current_exe().map_err(|error| failed(&error))?;
    let link = directory.join(format!("gh{}", std::env::consts::EXE_SUFFIX));
    if !link.exists() {
        #[cfg(unix)]
        std::os::unix::fs::symlink(&this, &link).map_err(|error| failed(&error))?;
        #[cfg(not(unix))]
        std::fs::copy(&this, &link).map_err(|error| failed(&error))?;
    }
    Ok(link)
}

/// Point `command` at the proxy for `phase`.
pub fn wire(command: &mut Command, wiring: &Wiring, phase: &str, deny: bool) {
    command
        .env("PATH", &wiring.search_path)
        .env("ONEVCS_GH", &wiring.proxy)
        .env(LOG, &wiring.log)
        .env(PHASE, phase)
        .env_remove(DENY);
    if let Some(real) = &wiring.real {
        command.env(REAL, real);
    }
    if deny {
        command
            .env(DENY, "1")
            .env_remove("GH_TOKEN")
            .env_remove("GITHUB_TOKEN");
    }
}

/// Where a run's proxy lives.
pub struct Wiring {
    /// The proxy's `gh`.
    pub proxy: PathBuf,
    /// The search path its callers use.
    pub search_path: OsString,
    /// The log of every call.
    pub log: PathBuf,
    /// The `gh` it forwards to, when this runner has one.
    pub real: Option<OsString>,
}

/// The `gh` this process was given: `ONEVCS_GH` when set, as onevcs itself honours it.
pub fn real_gh() -> Option<OsString> {
    std::env::var_os("ONEVCS_GH").or_else(|| {
        crate::tools::on_path("gh", &std::env::var_os("PATH").unwrap_or_default())
            .map(PathBuf::into_os_string)
    })
}

/// The proxy's own entry point.
pub fn run() -> ExitCode {
    let arguments: Vec<OsString> = std::env::args_os().skip(1).collect();
    let is_api = arguments.first().is_some_and(|verb| verb == "api");
    let local_only = arguments.first().is_none_or(|verb| {
        ["--version", "version", "--help", "help"].contains(&verb.to_string_lossy().as_ref())
    });
    if !local_only && std::env::var_os(DENY).is_some() {
        append(&unreached(Outcome::NoCredential));
        eprintln!("gh: this run gives budget commands no GitHub credential");
        return ExitCode::from(4);
    }
    let Some(real) = std::env::var_os(REAL) else {
        append(&unreached(Outcome::NoGh));
        eprintln!("gh: not installed on this runner; install the GitHub CLI");
        return ExitCode::from(127);
    };
    let mut retried = false;
    loop {
        let mut answer = forward(&real, &arguments);
        let classified = classify(&answer);
        let exchanges = take_exchanges(&mut answer.stderr);
        record(&exchanges, &classified, is_api && !local_only, retried);
        let may_retry = !retried && retries_spent() < MAX_RETRIES_PER_RUN;
        match classified.retry_after {
            Some(wait) if is_api && may_retry && wait <= MAX_RETRY_WAIT_SECONDS => {
                std::thread::sleep(Duration::from_secs(wait));
                retried = true;
            }
            _ => return exit_as(&answer),
        }
    }
}

/// Log one invocation's exchanges with the GitHub API: its outcome belongs to the last one,
/// since `gh` stops at the first it cannot go on from. An invocation that sent nothing leaves
/// a line only when it failed, because that failure may be why a measurement has no value.
fn record(exchanges: &[String], classified: &Classified, is_api: bool, retried: bool) {
    let api: Vec<&String> = exchanges.iter().filter(|url| is_api_url(url)).collect();
    for (position, url) in api.iter().enumerate() {
        let last = position + 1 == api.len();
        append(&json!({
            "phase": std::env::var(PHASE).unwrap_or_default(),
            "budget": std::env::var("ONEBUDGETSPEC_BUDGET_ID").ok(),
            "path": url.split('?').next().unwrap_or_default(),
            "outcome": if last { classified.outcome } else { Outcome::Answered }.as_str(),
            "status": if last { classified.status } else { None },
            "reached": true,
            "retry": retried && position == 0,
        }));
    }
    if !api.is_empty() {
        return;
    }
    if is_api && classified.outcome == Outcome::Answered {
        // `gh api` answered, so it sent at least this one request, in a form this cannot read.
        let mut line = unreached(Outcome::Answered);
        line["reached"] = json!(true);
        line["uninstrumented"] = json!(true);
        append(&line);
    } else if classified.outcome != Outcome::Answered {
        append(&unreached(classified.outcome));
    }
}

/// Object storage behind a redirect is a file transfer, not a GitHub API request.
fn is_api_url(url: &str) -> bool {
    let Some((_, rest)) = url.split_once("://") else {
        return false;
    };
    let (host, path) = rest.split_once('/').unwrap_or((rest, ""));
    host.starts_with("api.") || path.starts_with("api/")
}

/// Remove `gh`'s request lines from what it wrote, and answer with their URLs. A caller that
/// asked for debug output itself keeps it.
fn take_exchanges(stderr: &mut Vec<u8>) -> Vec<String> {
    let text = String::from_utf8_lossy(stderr).into_owned();
    let urls = text
        .lines()
        .filter_map(|line| line.strip_prefix(REQUEST_LINE))
        .map(str::to_owned)
        .collect();
    if !caller_wants_debug() {
        let kept: String = text
            .split_inclusive('\n')
            .filter(|line| !line.starts_with(GH_DEBUG_LINES))
            .collect();
        *stderr = kept.into_bytes();
    }
    urls
}

/// Whether the caller's own `GH_DEBUG` already turns `gh`'s debug output on.
fn caller_wants_debug() -> bool {
    std::env::var("GH_DEBUG").is_ok_and(|value| !["", "0", "false", "no"].contains(&value.as_str()))
}

fn forward(real: &OsString, arguments: &[OsString]) -> Output {
    let mut command = Command::new(real);
    command
        .args(arguments)
        // Forced colour would wrap the lines this reads in escape codes.
        .env("NO_COLOR", "1")
        .env_remove("CLICOLOR_FORCE");
    if !caller_wants_debug() {
        command.env("GH_DEBUG", "1");
    }
    match command.output() {
        Ok(output) => output,
        Err(error) => {
            eprintln!("gh: cannot run the GitHub CLI: {error}");
            std::process::exit(127);
        }
    }
}

fn exit_as(answer: &Output) -> ExitCode {
    // The caller reads what it can; a closed stream is its own to report.
    let _ = std::io::stdout().write_all(&answer.stdout);
    let _ = std::io::stderr().write_all(&answer.stderr);
    ExitCode::from(
        answer
            .status
            .code()
            .map_or(1, |code| u8::try_from(code).unwrap_or(1)),
    )
}

struct Classified {
    outcome: Outcome,
    status: Option<u16>,
    retry_after: Option<u64>,
}

/// Read one answer: `gh api --include` shows the status and headers, and without it only
/// the `HTTP nnn` of its error line is left.
fn classify(answer: &Output) -> Classified {
    let stdout = String::from_utf8_lossy(&answer.stdout);
    let stderr = String::from_utf8_lossy(&answer.stderr);
    let (head, body) = stdout
        .split_once("\r\n\r\n")
        .or_else(|| stdout.split_once("\n\n"))
        .unwrap_or_default();
    let header = |name: &str| {
        head.lines().skip(1).find_map(|line| {
            let (key, value) = line.split_once(':')?;
            key.trim()
                .eq_ignore_ascii_case(name)
                .then(|| value.trim().to_owned())
        })
    };
    let status = head
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse().ok())
        .or_else(|| stderr_status(&stderr));
    if answer.status.success() {
        return Classified {
            outcome: Outcome::Answered,
            status,
            retry_after: None,
        };
    }
    let retry_after = header("retry-after").and_then(|value| value.parse().ok());
    let text = format!("{body} {stderr}").to_lowercase();
    let limited = status == Some(429)
        || (status == Some(403)
            && (retry_after.is_some()
                || header("x-ratelimit-remaining").as_deref() == Some("0")
                || text.contains("rate limit")));
    let outcome = if limited {
        Outcome::RateLimited
    } else if status == Some(401) || text.contains("bad credentials") {
        Outcome::Unauthorized
    } else {
        Outcome::Failed
    };
    Classified {
        outcome,
        status,
        retry_after: if limited { retry_after } else { None },
    }
}

fn stderr_status(stderr: &str) -> Option<u16> {
    let (_, after) = stderr.split_once("HTTP ")?;
    after.get(..3)?.parse().ok()
}

/// A log line for an invocation no exchange of which can be named. Only a request's path is
/// ever kept elsewhere: its fields can hold a whole comment body.
fn unreached(outcome: Outcome) -> Value {
    json!({
        "phase": std::env::var(PHASE).unwrap_or_default(),
        "budget": std::env::var("ONEBUDGETSPEC_BUDGET_ID").ok(),
        "path": "",
        "outcome": outcome.as_str(),
        "status": null,
        "reached": false,
        "retry": false,
    })
}

fn append(line: &Value) {
    let Some(path) = std::env::var_os(LOG) else {
        return;
    };
    let written = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .and_then(|mut file| writeln!(file, "{line}"));
    if let Err(error) = written {
        eprintln!("gh: cannot record this request: {error}");
    }
}

fn retries_spent() -> usize {
    std::env::var_os(LOG).map_or(0, |path| {
        read_log(Path::new(&path))
            .iter()
            .filter(|line| line["retry"] == true)
            .count()
    })
}

/// Every call logged so far.
pub fn read_log(path: &Path) -> Vec<Value> {
    std::fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .filter_map(|line| serde_json::from_str(line).ok())
        .collect()
}

/// Why `budget`'s failed measurement says nothing about the budget: its GitHub read was
/// refused or impossible, which is missing input rather than a measurement.
pub fn blocked_reason(log: &[Value], budget: &str) -> Option<String> {
    let line = log
        .iter()
        .rev()
        .find(|line| line["budget"] == budget && line["outcome"] != Outcome::Answered.as_str())?;
    match Outcome::parse(line["outcome"].as_str()?)? {
        Outcome::RateLimited => Some(format!(
            "GitHub refused its read for a rate limit (HTTP {}): measurement unavailable",
            line["status"]
        )),
        Outcome::Unauthorized => {
            Some("GitHub refused this run's credential: measurement unavailable".into())
        }
        Outcome::NoCredential => Some("this run gives budget commands no GitHub credential".into()),
        Outcome::NoGh => {
            Some("its command needs the GitHub CLI, which this runner does not have".into())
        }
        Outcome::Answered | Outcome::Failed => None,
    }
}
