//! The facts of one blob, written as JSON lines.

use std::collections::BTreeSet;

use gix::ObjectId;
#[cfg(test)]
use libc::LC_CTYPE;
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
use crate::request::NameWordClass;
use crate::tokenizer;
use crate::tokenizer::Token;
use crate::unicode_tables::LINE_BOUNDARIES;

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
    MissingBlob,
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
    word_class: NameWordClass,
) -> Result<String, Error> {
    let mut lines = String::new();
    let blob = blob.to_string();
    if kinds == [FactKind::Names]
        && queries.is_empty()
        && matches!(word_class, NameWordClass::Ascii)
    {
        append_line(
            &mut lines,
            &FactLine {
                blob:    &blob,
                fact:    FactKind::Names,
                reading: Reading::Value(FactValue::Names(ascii_names(data))),
            },
        )?;
        return Ok(lines);
    }
    let text: Result<Vec<char>, Unreadable> = std::str::from_utf8(data)
        .map(|text| text.chars().collect())
        .map_err(|_| Unreadable::NotUtf8);
    let Ok(text) = text else {
        return not_utf8_readings(&blob, data, kinds, queries, word_class);
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
            FactKind::Names => Reading::Value(FactValue::Names(match word_class {
                NameWordClass::PosixAlnum => names(&text),
                NameWordClass::Ascii => ascii_names(data),
            })),
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

/// Keep ASCII names readable when the other requested facts require UTF-8.
fn not_utf8_readings(
    blob: &str,
    data: &[u8],
    kinds: &[FactKind],
    queries: &[&ModuleSitesQuery],
    word_class: NameWordClass,
) -> Result<String, Error> {
    let mut lines = String::new();
    let readings = kinds.iter().map(|&fact| {
        let reading = if fact == FactKind::Names && matches!(word_class, NameWordClass::Ascii) {
            Reading::Value(FactValue::Names(ascii_names(data)))
        } else {
            Reading::Unreadable(Unreadable::NotUtf8)
        };
        (fact, reading)
    });
    append_unreadable(&mut lines, blob, readings, queries, Unreadable::NotUtf8)?;
    Ok(lines)
}

/// Report absent blobs without discarding the other search readings.
pub(crate) fn missing_readings(
    blob: ObjectId,
    kinds: &[FactKind],
    queries: &[&ModuleSitesQuery],
) -> Result<String, Error> {
    let mut lines = String::new();
    let blob = blob.to_string();
    let readings = kinds
        .iter()
        .map(|&fact| (fact, Reading::Unreadable(Unreadable::MissingBlob)));
    append_unreadable(
        &mut lines,
        &blob,
        readings,
        queries,
        Unreadable::MissingBlob,
    )?;
    Ok(lines)
}

/// Write the supplied fact readings and the reason for unreadable module queries.
fn append_unreadable<'a>(
    lines: &mut String,
    blob: &str,
    readings: impl Iterator<Item = (FactKind, Reading<FactValue<'a>>)>,
    queries: &[&ModuleSitesQuery],
    reason: Unreadable,
) -> Result<(), Error> {
    for (fact, reading) in readings {
        append_line(
            lines,
            &FactLine {
                blob,
                fact,
                reading,
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
                reading: Reading::Unreadable(reason),
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

unsafe extern "C" {
    fn iswalnum(character: u32) -> libc::c_int;
}

/// Each longest run of the host POSIX alphanumeric class and underscore.
fn names(text: &[char]) -> BTreeSet<String> {
    text.split(|&character| {
        // SAFETY: Each Rust character is a valid code point for the host wide-character classifier.
        character != '_' && unsafe { iswalnum(u32::from(character)) } == 0
    })
    .filter(|word| !word.is_empty())
    .map(|word| word.iter().collect())
    .collect()
}

/// ASCII words remain readable when other bytes are not UTF-8.
fn ascii_names(data: &[u8]) -> BTreeSet<String> {
    data.split(|byte| !byte.is_ascii_alphanumeric() && *byte != b'_')
        .filter(|word| !word.is_empty())
        .map(|word| String::from_utf8_lossy(word).into_owned())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::LC_CTYPE;
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
        let selected = [c"C.UTF-8", c"en_US.UTF-8"].into_iter().any(|locale| {
            // SAFETY: No other unit test changes or reads the C locale.
            !unsafe { libc::setlocale(LC_CTYPE, locale.as_ptr()) }.is_null()
        });
        assert!(selected, "the test needs an available UTF-8 locale");
        let found = names(&characters("let naïve_1 = \"x::y\"; // ü"));
        let expected = ["let", "naïve_1", "x", "y", "ü"].map(str::to_owned);
        assert_eq!(found, expected.into_iter().collect());
    }
}
