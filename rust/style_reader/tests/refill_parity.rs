//! Compare the Python and Rust ledger paths with one real reader binary.

use std::path::Path;
use std::process::Command;

use serde_json::Value;
use tempfile::TempDir;

mod support;

use support::ReapedChild;
use support::TestResult;
use support::git;

fn compare(repository: &Path) -> TestResult<()> {
    let checkout = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .ok_or("the package has no checkout root")?;
    let output = ReapedChild::spawn(
        Command::new(checkout.join("scripts/lib/py"))
            .current_dir(checkout)
            .args(["-m", "scripts.style.style_reader_parity"])
            .arg(repository)
            .arg(env!("CARGO_BIN_EXE_style_reader")),
    )?
    .output()?;
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let result: Value = serde_json::from_slice(&output.stdout)?;
    assert_eq!(result["python"], result["rust"]);
    Ok(())
}

#[test]
fn refill_trees_and_names_match_python_with_markdown_and_missing_blobs() -> TestResult<()> {
    let directory = TempDir::new()?;
    let repository = directory.path();
    git(repository, &["init", "-q", "--initial-branch=main"])?;
    git(repository, &["config", "user.name", "Reader parity"])?;
    git(
        repository,
        &["config", "user.email", "reader@example.invalid"],
    )?;
    std::fs::create_dir_all(repository.join("nested/deep"))?;
    std::fs::create_dir_all(repository.join("docs"))?;
    for (path, text) in [
        (
            "Cargo.toml",
            "[package]\nname = \"fixture\"\nversion = \"0.1.0\"\n",
        ),
        (
            "nested.rs",
            "fn working() {} // café 引数 éshared T² ²shared xⅧ\n",
        ),
        ("nested/lib.rs", "fn shared() {}\n"),
        ("nested/deep/duplicate.rs", "fn shared() {}\n"),
        (
            "docs/words.md",
            "shared café caféine 引数 引数値 r#type T² ²shared xⅧ\n",
        ),
        ("docs/empty.md", ""),
        ("missing.rs", "fn only_in_missing_blob() {}\n"),
    ] {
        std::fs::write(repository.join(path), text)?;
    }
    std::fs::write(repository.join("bytes.rs"), b"// shared \xff\n")?;
    git(repository, &["add", "."])?;
    git(repository, &["update-index", "--chmod=+x", "nested.rs"])?;
    git(repository, &["commit", "-qm", "Create the reader fixture"])?;
    compare(repository)?;
    let blob = git(repository, &["rev-parse", "HEAD:missing.rs"])?;
    std::fs::remove_file(
        repository
            .join(".git/objects")
            .join(&blob[..2])
            .join(&blob[2..]),
    )?;
    compare(repository)?;
    Ok(())
}
