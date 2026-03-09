mod codex;
mod events;

use codex::{run, Mode, RunConfig};

fn main() {
    match parse_args().and_then(run) {
        Ok(code) => std::process::exit(code),
        Err(message) => {
            eprintln!("{message}");
            std::process::exit(1);
        }
    }
}

fn parse_args() -> Result<RunConfig, String> {
    let mut args = std::env::args().skip(1);
    let mut workspace_root: Option<String> = None;
    let mut codex_bin: Option<String> = None;
    let mut codex_args: Vec<String> = Vec::new();

    loop {
        let Some(arg) = args.next() else {
            return Err("missing mode".to_string());
        };
        match arg.as_str() {
            "--workspace-root" => {
                workspace_root = args.next();
                if workspace_root.is_none() {
                    return Err("missing value for --workspace-root".to_string());
                }
            }
            "--codex-bin" => {
                codex_bin = args.next();
                if codex_bin.is_none() {
                    return Err("missing value for --codex-bin".to_string());
                }
            }
            "--codex-arg" => {
                let Some(value) = args.next() else {
                    return Err("missing value for --codex-arg".to_string());
                };
                codex_args.push(value);
            }
            "exec" => {
                let prompt = parse_prompt(&mut args)?;
                return Ok(RunConfig {
                    workspace_root: workspace_root.ok_or("missing --workspace-root".to_string())?,
                    codex_bin: codex_bin.ok_or("missing --codex-bin".to_string())?,
                    codex_args,
                    mode: Mode::Exec { prompt },
                });
            }
            "resume" => {
                let thread_id = args.next().ok_or("missing thread id for resume".to_string())?;
                let prompt = parse_prompt(&mut args)?;
                return Ok(RunConfig {
                    workspace_root: workspace_root.ok_or("missing --workspace-root".to_string())?,
                    codex_bin: codex_bin.ok_or("missing --codex-bin".to_string())?,
                    codex_args,
                    mode: Mode::Resume { thread_id, prompt },
                });
            }
            other => return Err(format!("unexpected argument: {other}")),
        }
    }
}

fn parse_prompt(args: &mut impl Iterator<Item = String>) -> Result<String, String> {
    match args.next().as_deref() {
        Some("--") => {}
        Some(other) => return Err(format!("expected `--` before prompt, got {other}")),
        None => return Err("missing `--` before prompt".to_string()),
    }
    args.next().ok_or("missing prompt".to_string())
}
