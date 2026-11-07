//! The action's driver and the direct tool: one report operation, inside Actions or not.
//!
//! Everything it learns arrives as an argument, so no Actions variable is ever read; the
//! action's `action.yml` is what maps the workflow context onto these flags.
mod catalog;
mod cli;
mod finish;
mod merged;
mod proxy;
mod report;
mod saved;
mod state;
mod telemetry;
mod tools;

use std::io::{self, Write};
use std::process::ExitCode;

use clap::{CommandFactory, Parser};

use cli::{Cli, Command};

fn main() -> ExitCode {
    if proxy::invoked_as_gh() {
        return proxy::run();
    }
    if help_or_version_with_more() {
        let refused = writeln!(
            io::stderr().lock(),
            "error: unsupported arguments; use --help"
        );
        return match refused {
            Ok(()) => ExitCode::from(2),
            Err(failure) => output_failed(&failure),
        };
    }
    let cli = match Cli::try_parse() {
        Ok(cli) => cli,
        Err(error) => return answer_parse_failure(&error),
    };
    let Some(command) = cli.command else {
        return match Cli::command().print_help() {
            Ok(()) => ExitCode::SUCCESS,
            Err(failure) => output_failed(&failure),
        };
    };
    let outcome = match command {
        Command::Report(args) => report::run(&args),
        Command::Finish(args) => finish::run(&args),
        Command::Reproduce(args) => report::reproduce(&args),
        Command::Telemetry(args) => telemetry::read(&args),
    };
    match outcome {
        Ok(code) => code,
        Err(failure) => {
            // Reporting is best-effort when the diagnostic stream itself is broken.
            let _ = writeln!(io::stderr().lock(), "error: {failure}");
            ExitCode::from(failure.code())
        }
    }
}

/// `--help` or `--version` beside anything else is refused rather than answered, as the
/// released command line always has.
fn help_or_version_with_more() -> bool {
    let arguments: Vec<_> = std::env::args_os().skip(1).collect();
    arguments.len() > 1
        && matches!(
            arguments[0].to_str(),
            Some("--help" | "-h" | "--version" | "-V")
        )
}

/// Help and version answer with success; anything else unsupported is exit 2 having done
/// no work, and a stream that cannot take the answer is exit 1.
fn answer_parse_failure(error: &clap::Error) -> ExitCode {
    use clap::error::ErrorKind::{DisplayHelp, DisplayVersion};
    if matches!(error.kind(), DisplayHelp | DisplayVersion) {
        return match error.print() {
            Ok(()) => ExitCode::SUCCESS,
            Err(failure) => output_failed(&failure),
        };
    }
    let printed = writeln!(
        io::stderr().lock(),
        "error: unsupported arguments; use --help\n{error}"
    );
    match printed {
        Ok(()) => ExitCode::from(2),
        Err(failure) => output_failed(&failure),
    }
}

fn output_failed(failure: &io::Error) -> ExitCode {
    let _ = writeln!(io::stderr().lock(), "error: output failed: {failure}");
    ExitCode::FAILURE
}
