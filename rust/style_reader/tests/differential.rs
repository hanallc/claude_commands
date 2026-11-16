//! Compare every reader fact with the production Python readers.

use std::collections::BTreeMap;
use std::error::Error;
use std::io::Write;
use std::path::Path;
use std::process::Child;
use std::process::Command;
use std::process::Stdio;

use serde_json::Value;
use serde_json::json;
use tempfile::TempDir;

const MODULES: &str = concat!(
    "mod lock;\n",
    "#[path = \"lock.rs\"] mod direct;\n",
    "#[path = \"lo\\x63k.rs\"] mod escaped;\n",
    "#[cfg_attr(all(), path = \"lock.rs\")] mod conditional;\n",
    "mod outer { #[path = \"child.rs\"] pub mod child; mod inner { mod leaf; } }\n",
    "fn work() { mod nested; }\n",
    "mod r#raw;\n",
    "make! { mod not_a_module; }\n",
);
const PATHS: &str = concat!(
    "mod lock;\n",
    "use crate::{lock::{self, Value as Alias, *}, other};\n",
    "use self::lock as alias;\n",
    "use super::{lock, sibling::*};\n",
    "use sample_library::lock::Value;\n",
    "fn work() { crate::lock::Value; self::lock::Value; super::lock::Value; lock::Value; alias::Value; }\n",
    "mod nested { use super::*; fn run() { lock::Value; } }\n",
    "// See [`crate::lock::Value`], self::lock and super::lock.\n",
    "const TEXT: &str = \"crate::lock::Value\";\n",
);
const ITEMS: &str = concat!(
    "#[cfg(test)]\n#[inline]\npub const fn r#work() { r#work(); }\n",
    "static mut STATE: u32 = 1;\nconst _: u32 = 2;\n",
    "struct Value<T> { value: T }\nenum Kind { First }\nunion Bits { value: u32 }\n",
    "type Number = u32;\nmod inline { pub fn child() { child(); } }\n",
    "trait Trait<T> { type Output; const LIMIT: u32 = 3; fn call(&self); fn defaulted() { defaulted(); } }\n",
    "impl<T> Value<T> { #[inline] fn value(&self) { value(); } }\n",
    "impl<T> path::Trait<T> for path::Value<T> where T: Copy { fn call(&self) { call(); } }\n",
    "fn host() { use path::Trait; impl Trait<u32> for Value<u32> { fn nested() { nested(); } } }\n",
    "impl dyn Trait for &Value<u32> { fn unreadable_owner() {} }\n",
    "impl Trait() for Value<u32> { fn unreadable_trait() {} }\n",
    "impl Value<u32> + Extra { fn unreadable_type() {} }\n",
    "impl<T: for<'a> Trait<'a>> Other<T> { fn higher_ranked() {} }\n",
);
const BODIES: &str = concat!(
    "fn same() { same(); let text = \"same\"; /* same */ let number = 1; }\n",
    "fn comments() { // comments\n /* comments */\n}\n",
    "fn unicode() { let text = \"é 😀\"; unicode(); }\n",
    "fn lines() {\r\n\r\n    lines();\r\n}\r\n",
    "fn separators() {\u{85}separators();\u{2028}}\n",
    "fn comments_with_cr() { // comment\rcomments_with_cr(); }\n",
    "fn comments_with_ff() { // comment\u{c}comments_with_ff(); }\n",
);
const BROKEN: &str = "fn cafe() { let café = 1; }\nfn unfinished() { /* no end";

type TestResult<T> = Result<T, Box<dyn Error>>;

fn git(repository: &Path, arguments: &[&str]) -> TestResult<String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(repository)
        .args(arguments)
        .output()?;
    assert!(
        output.status.success(),
        "Git {arguments:?}: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    Ok(String::from_utf8(output.stdout)?.trim().to_owned())
}

/// Reap the reader after an input error or a panic.
struct ReaderChild(Child);

impl Drop for ReaderChild {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn output_lines(mut command: Command, request: &Value) -> TestResult<BTreeMap<String, Value>> {
    let mut child = ReaderChild(
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()?,
    );
    {
        let mut input = child.0.stdin.take().ok_or("the reader has no stdin")?;
        serde_json::to_writer(&mut input, request)?;
        input.flush()?;
    }
    let mut stdout = child.0.stdout.take().ok_or("the reader has no stdout")?;
    let mut stderr = child.0.stderr.take().ok_or("the reader has no stderr")?;
    let (stdout, stderr) = std::thread::scope(|scope| {
        let errors = scope.spawn(move || {
            let mut bytes = Vec::new();
            std::io::Read::read_to_end(&mut stderr, &mut bytes).map(|_| bytes)
        });
        let mut bytes = Vec::new();
        std::io::Read::read_to_end(&mut stdout, &mut bytes).inspect_err(|_| {
            let _ = child.0.kill();
        })?;
        let errors = errors.join().map_err(|_| "the stderr reader panicked")??;
        Ok::<_, Box<dyn Error>>((bytes, errors))
    })?;
    let output = std::process::Output {
        status: child.0.wait()?,
        stdout,
        stderr,
    };
    assert!(
        output.status.success(),
        "reader failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let mut lines = BTreeMap::new();
    for line in String::from_utf8(output.stdout)?.lines() {
        let value: Value = serde_json::from_str(line)?;
        let key = format!("{} {} {}", value["blob"], value["fact"], value["path"]);
        assert!(
            lines.insert(key.clone(), value).is_none(),
            "duplicate fact: {key}"
        );
    }
    Ok(lines)
}

fn compare(request: &Value) -> TestResult<()> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .ok_or("the rust folder is missing")?
        .parent()
        .ok_or("the repository root is missing")?;
    let rust = output_lines(Command::new(env!("CARGO_BIN_EXE_style_reader")), request)?;
    let mut python = Command::new("python3");
    python
        .current_dir(root)
        .args(["-m", "scripts.style.style_reader_oracle"]);
    let expected = output_lines(python, request)?;
    assert_eq!(
        rust.keys().collect::<Vec<_>>(),
        expected.keys().collect::<Vec<_>>(),
        "fact keys"
    );
    for (key, value) in expected {
        assert_eq!(rust[&key], value, "fact {key}");
    }
    Ok(())
}

#[test]
fn all_facts_match_python_on_declarations_paths_bodies_and_unreadable_source() -> TestResult<()> {
    let directory = TempDir::new()?;
    let repository = directory.path();
    git(repository, &["init", "-q"])?;
    git(repository, &["config", "user.name", "Reader Test"])?;
    git(
        repository,
        &["config", "user.email", "reader@example.invalid"],
    )?;
    for (name, source) in [
        ("modules.rs", MODULES),
        ("paths.rs", PATHS),
        ("items.rs", ITEMS),
        ("bodies.rs", BODIES),
        (
            "equal_items.rs",
            "#[cfg(unix)] fn work() { work(); } #[cfg(windows)] fn work() { let n=1; work(); }",
        ),
        ("broken.rs", BROKEN),
        ("empty.rs", ""),
        ("comment.rs", "// crate::lock::Value\n"),
        (
            "uncertain.rs",
            "#[cfg_attr(all(), path = \"lock.rs\")] mod lock;",
        ),
        (
            "no_body.rs",
            "fn declared(); struct Tuple(u32); const EMPTY: u32 = 1;",
        ),
        ("incomplete.rs", "fn incomplete() { incomplete();"),
    ] {
        std::fs::write(repository.join(name), source)?;
    }
    std::fs::write(repository.join("not_utf8.rs"), b"fn invalid() {}\xff")?;
    std::fs::write(repository.join("ignored.txt"), "fn excluded() {}")?;
    git(repository, &["add", "."])?;
    git(repository, &["commit", "-qm", "Add reader fixtures"])?;
    let first = git(repository, &["rev-parse", "HEAD"])?;
    std::fs::write(
        repository.join("history.rs"),
        "fn historical() { historical(); }",
    )?;
    git(repository, &["add", "."])?;
    git(repository, &["commit", "-qm", "Add another reader fixture"])?;
    let second = git(repository, &["rev-parse", "HEAD"])?;
    let moves = json!([
        {"old":"src/lock.rs", "new":"src/mutation_lock.rs", "file_module":{"crate":[]}, "parent":[], "library":{"name":"sample_library"}},
        {"old":"src/lock.rs", "new":"src/mutation_lock.rs", "file_module":"outside_crate", "parent":[], "library":{"name":"sample_library"}},
        {"old":"src/lock.rs", "new":"src/mutation_lock.rs", "file_module":"outside_crate", "parent":[], "library":"none"},
        {"old":"src/outer/lock.rs", "new":"src/outer/mutation_lock.rs", "file_module":{"crate":["outer","child"]}, "parent":["outer"], "library":"none"}
    ]);
    let mut queries: Vec<Value> = [
        "paths.rs",
        "modules.rs",
        "comment.rs",
        "uncertain.rs",
        "broken.rs",
        "not_utf8.rs",
    ]
    .into_iter()
    .map(|name| {
        let blob = git(repository, &["rev-parse", &format!("HEAD:{name}")])?;
        Ok(json!({"blob":blob, "path":"src/lib.rs", "moves":moves}))
    })
    .collect::<TestResult<Vec<_>>>()?;
    std::fs::write(repository.join("query_only.rs"), "crate::lock::Value;")?;
    let query_blob = git(repository, &["hash-object", "-w", "query_only.rs"])?;
    std::fs::remove_file(repository.join("query_only.rs"))?;
    queries.push(json!({"blob":query_blob, "path":"src/query_only.rs", "moves":moves}));
    compare(&json!({
        "repository":repository, "commits":[first,second],
        "facts":["tokens","lines","names","module_scan","declarations","name_sites"],
        "module_sites":queries
    }))
}
