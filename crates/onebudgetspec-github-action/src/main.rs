//! Placeholder command line; performs no operational work.
use std::io::{self, Write};
use std::process::ExitCode;

fn main() -> ExitCode {
    match run() {
        Ok(status) => status,
        Err(error) => {
            let _ = writeln!(io::stderr().lock(), "error: output failed: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> io::Result<ExitCode> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    match args.as_slice() {
        [] => {
            help()?;
            Ok(ExitCode::SUCCESS)
        }
        [arg] if arg == "--help" || arg == "-h" => {
            help()?;
            Ok(ExitCode::SUCCESS)
        }
        [arg] if arg == "--version" || arg == "-V" => {
            writeln!(
                io::stdout().lock(),
                "onebudgetspec-github-action {}",
                env!("CARGO_PKG_VERSION")
            )?;
            Ok(ExitCode::SUCCESS)
        }
        _ => {
            writeln!(
                io::stderr().lock(),
                "error: unsupported arguments; use --help"
            )?;
            Ok(ExitCode::from(2))
        }
    }
}

fn help() -> io::Result<()> {
    writeln!(
        io::stdout().lock(),
        "onebudgetspec-github-action — scaffold only\n\nUsage: onebudgetspec-github-action [--help | --version]\n\nNo operational commands are implemented.\nExit codes: 0 success; 1 output error; 2 unsupported arguments."
    )
}
