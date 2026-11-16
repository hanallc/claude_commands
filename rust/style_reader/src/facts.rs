//! The facts of one blob, written as JSON lines.

use std::collections::BTreeSet;

use gix::ObjectId;
use serde::Serialize;
use serde_json::Error;

use crate::constants::CARRIAGE_RETURN_LINE_FEED;
use crate::constants::MODULE_SITES_FACT;
use crate::declarations;
use crate::declarations::DeclaredFile;
use crate::module_scan;
use crate::module_scan::ModuleScan;
use crate::module_scan::ModuleScanReading;
use crate::module_sites;
use crate::module_sites::ModuleSiteReading;
use crate::module_sites::ModuleSitesQuery;
use crate::name_sites;
use crate::name_sites::NameSiteReading;
use crate::request::FactKind;
use crate::tokenizer;
use crate::tokenizer::Token;
use crate::unicode_tables::LINE_BOUNDARIES;
use crate::unicode_tables::WORD;

/// One output line for one requested blob fact.
#[derive(Serialize)]
struct FactLine<'a> {
    blob:    &'a str,
    fact:    FactKind,
    #[serde(flatten)]
    reading: Reading<FactValue<'a>>,
}

/// One output line for a module site query.
#[derive(Serialize)]
struct ModuleSitesLine<'a> {
    blob:    &'a str,
    path:    &'a str,
    fact:    &'a str,
    #[serde(flatten)]
    reading: Reading<Vec<ModuleSiteReading>>,
}

/// A fact value, or the reason that the blob has no value.
#[derive(Serialize)]
#[serde(rename_all = "snake_case")]
enum Reading<Value> {
    Value(Value),
    Unreadable(Unreadable),
}

#[derive(Serialize)]
#[serde(untagged)]
enum FactValue<'a> {
    Lines(usize),
    Names(BTreeSet<String>),
    Tokens(&'a [Token]),
    ModuleScan(ModuleScan),
    Declarations(&'a DeclaredFile),
    NameSites(Vec<NameSiteReading>),
}

/// Why a blob has no facts.
#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
enum Unreadable {
    NotUtf8,
    TokenSpans,
}

/// Declarations read once for both declaration facts and name sites.
enum DeclarationReading {
    NotRequested,
    Read(DeclaredFile),
}

/// Facts and module queries share one token read of each blob.
pub(crate) fn blob_readings(
    blob: ObjectId,
    data: &[u8],
    kinds: &[FactKind],
    queries: &[&ModuleSitesQuery],
) -> Result<String, Error> {
    let text: Result<Vec<char>, Unreadable> = std::str::from_utf8(data)
        .map(|text| text.chars().collect())
        .map_err(|_| Unreadable::NotUtf8);
    let mut lines = String::new();
    let blob = blob.to_string();
    let Ok(text) = text else {
        append_unreadable(&mut lines, &blob, kinds, queries)?;
        return Ok(lines);
    };
    let token_kinds = kinds
        .iter()
        .any(|kind| !matches!(kind, FactKind::Lines | FactKind::Names));
    let tokens = if token_kinds || !queries.is_empty() {
        tokenizer::tokenize(&text)
    } else {
        Vec::new()
    };
    let declarations = if kinds
        .iter()
        .any(|kind| matches!(kind, FactKind::Declarations | FactKind::NameSites))
    {
        DeclarationReading::Read(declarations::read_tokens(&text, &tokens))
    } else {
        DeclarationReading::NotRequested
    };
    for &fact in kinds {
        let reading = match fact {
            FactKind::Lines => Reading::Value(FactValue::Lines(line_count(&text))),
            FactKind::Names => Reading::Value(FactValue::Names(names(&text))),
            FactKind::Tokens => Reading::Value(FactValue::Tokens(&tokens)),
            FactKind::ModuleScan => {
                let scan = module_scan::read_tokens(&tokens);
                match scan {
                    ModuleScanReading::Value(value) => Reading::Value(FactValue::ModuleScan(value)),
                    ModuleScanReading::TokenSpansUnreadable => {
                        Reading::Unreadable(Unreadable::TokenSpans)
                    },
                }
            },
            FactKind::Declarations | FactKind::NameSites => match &declarations {
                DeclarationReading::Read(file) if fact == FactKind::Declarations => {
                    Reading::Value(FactValue::Declarations(file))
                },
                DeclarationReading::Read(file) => {
                    Reading::Value(FactValue::NameSites(name_sites::read(&text, file)))
                },
                DeclarationReading::NotRequested => continue,
            },
        };
        append_line(
            &mut lines,
            &FactLine {
                blob: &blob,
                fact,
                reading,
            },
        )?;
    }
    for query in queries {
        let sites = module_sites::read_tokens(&text, &tokens, &query.path, &query.moves);
        append_line(
            &mut lines,
            &ModuleSitesLine {
                blob:    &blob,
                path:    &query.path,
                fact:    MODULE_SITES_FACT,
                reading: Reading::Value(sites),
            },
        )?;
    }
    Ok(lines)
}

/// Write unreadable output for every requested fact and query.
fn append_unreadable(
    lines: &mut String,
    blob: &str,
    kinds: &[FactKind],
    queries: &[&ModuleSitesQuery],
) -> Result<(), Error> {
    for &fact in kinds {
        append_line(
            lines,
            &FactLine {
                blob,
                fact,
                reading: Reading::Unreadable(Unreadable::NotUtf8),
            },
        )?;
    }
    for query in queries {
        append_line(
            lines,
            &ModuleSitesLine {
                blob,
                path: &query.path,
                fact: MODULE_SITES_FACT,
                reading: Reading::Unreadable(Unreadable::NotUtf8),
            },
        )?;
    }
    Ok(())
}

fn append_line(lines: &mut String, value: &impl Serialize) -> Result<(), Error> {
    lines.push_str(&serde_json::to_string(value)?);
    lines.push('\n');
    Ok(())
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
