//! `style_reader` (name TBD) reads facts about the Rust blobs of git commits for the style refill.
//!
//! It reads one JSON request on stdin: `{"repository": <path>, "commits": [<full ids>], "facts":
//! [<kinds>]}`. It writes one JSON line on stdout for each blob and fact kind, in blob id order:
//! `{"blob": <id>, "fact": <kind>, "value": <value>}`, or `"unreadable": "not_utf8"` in place of
//! the value. Each fact matches the Python reader in `scripts/style` that it ports.
//! `RAYON_NUM_THREADS` sets the thread count; the default is one thread for each CPU.

mod blob_reader;
mod constants;
mod declarations;
mod error;
mod facts;
mod module_scan;
mod module_sites;
mod name_sites;
mod request;
mod tokenizer;
mod unicode_tables;
mod word_syntax;

use std::error::Error;
use std::process::ExitCode;

use crate::request::Request;

fn main() -> ExitCode {
    match Request::read().and_then(Request::answer) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            let chain = std::iter::successors(Some(&error as &(dyn Error + 'static)), |&cause| {
                cause.source()
            })
            .map(ToString::to_string)
            .collect::<Vec<_>>()
            .join(": ");
            eprintln!("style_reader: {chain}");
            ExitCode::FAILURE
        },
    }
}
