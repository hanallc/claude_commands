//! The request that the reader reads from stdin, and its answer on stdout.

use std::io::BufWriter;
use std::io::Write;
use std::path::PathBuf;

use serde::Deserialize;
use serde::Serialize;

use crate::blob_reader::BlobReader;
use crate::error::ReaderError;
use crate::module_sites::ModuleSitesQuery;

/// The facts that a request asks for.
#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub(crate) enum FactKind {
    /// The number of lines that Python `str.splitlines` gives.
    Lines,
    /// Module declarations and their path attributes.
    ModuleScan,
    /// Declared items and their word spans.
    Declarations,
    /// Name offsets within each declared item body.
    NameSites,
    /// The words of the blob: each longest run of Python `\w` characters, in comments and literals
    /// too.
    Names,
    /// The code tokens of the Python token reader.
    Tokens,
}

/// Read every Rust blob at the commits of one repository.
#[derive(Debug, Deserialize)]
pub(crate) struct Request {
    repository:   PathBuf,
    commits:      Vec<String>,
    facts:        Vec<FactKind>,
    #[serde(default)]
    module_sites: Vec<ModuleSitesQuery>,
}

impl Request {
    /// Read one request from stdin.
    pub(crate) fn read() -> Result<Self, ReaderError> {
        serde_json::from_reader(std::io::stdin().lock()).map_err(ReaderError::Request)
    }

    /// Write the fact lines of every Rust blob at the commits to stdout.
    pub(crate) fn answer(self) -> Result<(), ReaderError> {
        let mut kinds = self.facts;
        kinds.sort_unstable();
        kinds.dedup();
        let reader = BlobReader::open(&self.repository)?;
        let blobs = reader.rust_blobs(&self.commits)?;
        let mut output = BufWriter::new(std::io::stdout().lock());
        reader.write_request(&blobs, &kinds, &self.module_sites, &mut output)?;
        output.flush().map_err(ReaderError::Write)
    }
}
