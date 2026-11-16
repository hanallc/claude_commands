//! The facts of one blob, written as JSON lines.

use std::collections::BTreeSet;

use gix::ObjectId;
use serde::Serialize;
use serde_json::Error;

use crate::constants::CARRIAGE_RETURN_LINE_FEED;
use crate::request::FactKind;
use crate::tokenizer;
use crate::tokenizer::Token;
use crate::unicode_tables::LINE_BOUNDARIES;
use crate::unicode_tables::WORD;

/// One output line: one fact kind of one blob.
#[derive(Serialize)]
struct FactLine {
    blob:    String,
    fact:    FactKind,
    #[serde(flatten)]
    reading: Reading,
}

/// A fact value, or why the blob has none.
#[derive(Serialize)]
#[serde(rename_all = "snake_case")]
enum Reading {
    Value(FactValue),
    Unreadable(Unreadable),
}

#[derive(Serialize)]
#[serde(untagged)]
enum FactValue {
    Lines(usize),
    Names(BTreeSet<String>),
    Tokens(Vec<Token>),
}

/// Why a blob has no facts. Python reads blobs with a strict UTF-8 decode.
#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
enum Unreadable {
    NotUtf8,
}

/// The JSON lines of the requested facts of one blob, in the order of kinds. Each kind is in
/// kinds once.
pub(crate) fn blob_facts(blob: ObjectId, data: &[u8], kinds: &[FactKind]) -> Result<String, Error> {
    let text: Result<Vec<char>, Unreadable> = std::str::from_utf8(data)
        .map(|text| text.chars().collect())
        .map_err(|_| Unreadable::NotUtf8);
    let mut lines = String::new();
    for &fact in kinds {
        let reading = text.as_deref().map_or_else(
            |&unreadable| Reading::Unreadable(unreadable),
            |text| {
                Reading::Value(match fact {
                    FactKind::Lines => FactValue::Lines(line_count(text)),
                    FactKind::Names => FactValue::Names(names(text)),
                    FactKind::Tokens => FactValue::Tokens(tokenizer::tokenize(text)),
                })
            },
        );
        lines.push_str(&serde_json::to_string(&FactLine {
            blob: blob.to_string(),
            fact,
            reading,
        })?);
        lines.push('\n');
    }
    Ok(lines)
}

/// Python `len(text.replace("\r\n", "\n").replace("\r", "\n").splitlines())`.
fn line_count(text: &[char]) -> usize {
    let boundaries = text
        .iter()
        .filter(|&&character| tokenizer::in_table(LINE_BOUNDARIES, character))
        .count();
    let carriage_return_pairs = text
        .windows(CARRIAGE_RETURN_LINE_FEED.len())
        .filter(|pair| *pair == CARRIAGE_RETURN_LINE_FEED)
        .count();
    let unended_last_line = text
        .last()
        .is_some_and(|&character| !tokenizer::in_table(LINE_BOUNDARIES, character));
    boundaries - carriage_return_pairs + usize::from(unended_last_line)
}

/// Each longest run of Python `\w` characters.
fn names(text: &[char]) -> BTreeSet<String> {
    text.split(|&character| !tokenizer::in_table(WORD, character))
        .filter(|word| !word.is_empty())
        .map(|word| word.iter().collect())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::line_count;
    use super::names;

    fn characters(text: &str) -> Vec<char> { text.chars().collect() }

    #[test]
    fn line_count_follows_python_splitlines() {
        for (text, expected) in [
            ("", 0),
            ("a", 1),
            ("a\n", 1),
            ("a\r\nb", 2),
            ("\r\r\n", 2),
            ("a\u{1c}b\u{2028}", 2),
            ("a\tb", 1),
        ] {
            assert_eq!(line_count(&characters(text)), expected, "{text:?}");
        }
    }

    #[test]
    fn names_are_word_runs_in_code_comments_and_literals() {
        let found = names(&characters("let naïve_1 = \"x::y\"; // ü"));
        let expected = ["let", "naïve_1", "x", "y", "ü"].map(str::to_owned);
        assert_eq!(found, expected.into_iter().collect());
    }
}
