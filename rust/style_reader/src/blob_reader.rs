//! Finds the Rust blobs of commits and reads their facts on every CPU.

use std::collections::BTreeMap;
use std::collections::BTreeSet;
use std::io::Write;
use std::path::Path;

use gix::ObjectId;
use gix::ThreadSafeRepository;
use gix::objs::tree::EntryKind;
use gix::traverse::tree::Recorder;
use rayon::prelude::*;

use crate::constants::BLOB_BATCH_SIZE;
use crate::constants::RUST_SUFFIX;
use crate::error::ReaderError;
use crate::facts;
use crate::module_sites::ModuleSitesQuery;
use crate::request::FactKind;

/// One repository, shared by the reader threads.
pub(crate) struct BlobReader {
    repository: ThreadSafeRepository,
}

impl BlobReader {
    pub(crate) fn open(path: &Path) -> Result<Self, ReaderError> {
        let repository = gix::open(path).map_err(|source| ReaderError::Open {
            path: path.to_owned(),
            source,
        })?;
        Ok(Self {
            repository: repository.into_sync(),
        })
    }

    /// The regular-file blobs whose path ends in `.rs` at any of the commits, each once, in id
    /// order.
    pub(crate) fn rust_blobs(&self, commits: &[String]) -> Result<Vec<ObjectId>, ReaderError> {
        let repository = self.repository.to_thread_local();
        let mut blobs = BTreeSet::new();
        for commit in commits {
            let id =
                ObjectId::from_hex(commit.as_bytes()).map_err(|source| ReaderError::CommitId {
                    commit: commit.clone(),
                    source,
                })?;
            let tree_error = |source: Box<dyn std::error::Error + Send + Sync>| ReaderError::Tree {
                commit: commit.clone(),
                source,
            };
            let tree = repository
                .find_commit(id)
                .map_err(|source| tree_error(Box::new(source)))?
                .tree()
                .map_err(|source| tree_error(Box::new(source)))?;
            let mut recorder = Recorder::default();
            tree.traverse()
                .breadthfirst(&mut recorder)
                .map_err(|source| tree_error(Box::new(source)))?;
            blobs.extend(
                recorder
                    .records
                    .into_iter()
                    .filter(|entry| {
                        matches!(
                            entry.mode.kind(),
                            EntryKind::Blob | EntryKind::BlobExecutable
                        )
                    })
                    .filter(|entry| entry.filepath.ends_with(RUST_SUFFIX))
                    .map(|entry| entry.oid),
            );
        }
        Ok(blobs.into_iter().collect())
    }

    /// Write all facts and queries in bounded batches, including queried blobs outside the commits.
    pub(crate) fn write_request(
        &self,
        blobs: &[ObjectId],
        kinds: &[FactKind],
        queries: &[ModuleSitesQuery],
        output: &mut impl Write,
    ) -> Result<(), ReaderError> {
        let mut grouped: BTreeMap<ObjectId, Vec<&ModuleSitesQuery>> = BTreeMap::new();
        for query in queries {
            let id = ObjectId::from_hex(query.blob.as_bytes()).map_err(|source| {
                ReaderError::QueryBlobId {
                    blob: query.blob.clone(),
                    source,
                }
            })?;
            grouped.entry(id).or_default().push(query);
        }
        let all: Vec<_> = blobs
            .iter()
            .copied()
            .chain(grouped.keys().copied())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        let requested: BTreeSet<_> = blobs.iter().copied().collect();
        all.chunks(BLOB_BATCH_SIZE).try_for_each(|batch| {
            let lines: Result<Vec<_>, _> = batch
                .par_iter()
                .map_init(
                    || self.repository.to_thread_local(),
                    |repository, &blob| {
                        let object =
                            repository
                                .find_blob(blob)
                                .map_err(|source| ReaderError::Blob {
                                    blob,
                                    source: Box::new(source),
                                })?;
                        let blob_kinds = if requested.contains(&blob) {
                            kinds
                        } else {
                            &[]
                        };
                        let blob_queries = grouped.get(&blob).map_or(&[][..], Vec::as_slice);
                        facts::blob_readings(blob, &object.data, blob_kinds, blob_queries)
                            .map_err(|source| ReaderError::Serialize { blob, source })
                    },
                )
                .collect();
            lines?.iter().try_for_each(|line| {
                output
                    .write_all(line.as_bytes())
                    .map_err(ReaderError::Write)
            })
        })
    }
}
